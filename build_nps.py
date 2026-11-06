#!/usr/bin/env python3
# coding: utf-8
# 生成插件图标及可上传宝塔的 ZIP 包；不包含测试代码和运行时凭据。
import ast
import binascii
import hashlib
import json
import re
import shutil
import struct
import tarfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / 'nps'
FILES = ('nps_main.py', 'index.html', 'install.sh', 'info.json', 'icon.png')


def build_icon():
    width = 80
    pixels = [bytearray([32, 165, 58] * width) for _ in range(width)]
    glyphs = [
        ['10001', '11001', '11001', '10101', '10011', '10011', '10001'],
        ['11110', '10001', '10001', '11110', '10000', '10000', '10000'],
        ['01111', '10000', '10000', '01110', '00001', '00001', '11110']
    ]
    for index, glyph in enumerate(glyphs):
        for y, row in enumerate(glyph):
            for x, value in enumerate(row):
                if value == '1':
                    for dy in range(3):
                        for dx in range(3):
                            px, py = 14 + index * 18 + x * 3 + dx, 29 + y * 3 + dy
                            pixels[py][px * 3:px * 3 + 3] = b'\xff\xff\xff'
    raw = b''.join(b'\x00' + bytes(row) for row in pixels)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind + data) & 0xffffffff)

    image = b'\x89PNG\r\n\x1a\n'
    image += chunk(b'IHDR', struct.pack('>IIBBBBB', width, width, 8, 2, 0, 0, 0))
    image += chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    (PLUGIN / 'icon.png').write_bytes(image)


def inspect_archive(path, version='v0.26.10'):
    role = 'nps' if path.name.endswith('_server.tar.gz') else 'npc'
    expected_machine = 62 if '_amd64_' in path.name else 183
    names = set()
    total = 0
    with tarfile.open(path, 'r:gz') as archive:
        for count, item in enumerate(archive):
            name = PurePosixPath(item.name)
            if name.is_absolute() or '..' in name.parts or '\\' in item.name or ':' in item.name:
                raise ValueError('本地安装包包含不安全路径：' + path.name)
            if not (item.isfile() or item.isdir()) or count > 20000:
                raise ValueError('本地安装包包含不支持的文件类型或过多文件：' + path.name)
            total += item.size
            if total > 600 * 1024 * 1024:
                raise ValueError('本地安装包解压后超过大小限制：' + path.name)
            if not item.isfile():
                continue
            if str(name) in names:
                raise ValueError('本地安装包包含重复文件：' + path.name)
            names.add(str(name))
            if str(name) == role:
                with archive.extractfile(item) as handle:
                    header = handle.read(20)
                    if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01'
                            or struct.unpack('<H', header[18:20])[0] != expected_machine):
                        raise ValueError('程序不是文件名对应架构的 Linux 64 位程序：' + path.name)
                    markers = (version.lstrip('v').encode('ascii'), b'ehang.io/nps')
                    found = [False] * len(markers)
                    tail = header
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        data = tail + block
                        found = [seen or marker in data for seen, marker in zip(found, markers)]
                        tail = data[-128:]
                if not all(found):
                    raise ValueError('本地程序来源或版本标识不匹配，需要 ehang-io/nps {}：{}'.format(version, path.name))
            elif role == 'nps' and str(name) == 'conf/nps.conf':
                if item.size > 1024 * 1024:
                    raise ValueError('服务端配置模板过大：' + path.name)
                with archive.extractfile(item) as handle:
                    config = handle.read().decode('utf-8-sig')
                if (not re.search(r'(?m)^\s*bridge_type\s*=\s*(tcp|kcp)\s*$', config)
                        or not re.search(r'(?m)^\s*bridge_port\s*=\s*[0-9]+\s*$', config)):
                    raise ValueError('服务端配置模板不是 ehang-io/nps 格式：' + path.name)
    required = {role, 'conf/' + role + '.conf'}
    if not required.issubset(names):
        raise ValueError('本地安装包缺少程序或配置：' + path.name)
    if role == 'nps' and not all(any(name.startswith(prefix) for name in names) for prefix in ('web/views/', 'web/static/')):
        raise ValueError('本地服务端安装包缺少 Web 界面资源：' + path.name)


def collect_packages(version='v0.26.10'):
    assets = {}
    for architecture in ('amd64', 'arm64'):
        for kind in ('server', 'client'):
            name = 'linux_{}_{}.tar.gz'.format(architecture, kind)
            path = PLUGIN / name
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 300 * 1024 * 1024:
                raise ValueError('本地安装包不是普通文件或大小不正确：' + name)
            inspect_archive(path, version)
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            assets[name] = {'size': path.stat().st_size, 'sha256': digest.hexdigest()}
    return assets


def merge_online_hashes(assets):
    # 可选文件：没有本地包时也能固定官方在线包的哈希。
    index = PLUGIN / 'online_hashes.json'
    if not index.exists() and not index.is_symlink():
        return assets
    if index.is_symlink() or not index.is_file() or not 0 < index.stat().st_size <= 64 * 1024:
        raise ValueError('online_hashes.json 不是普通文件或大小不正确')
    try:
        data = json.loads(index.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('online_hashes.json 格式错误')
    if not isinstance(data, dict):
        raise ValueError('online_hashes.json 必须是 包名 -> {size, sha256} 的对象')
    for name, entry in data.items():
        if not isinstance(name, str) or not re.fullmatch(r'linux_(amd64|arm64)_(server|client)\.tar\.gz', name):
            raise ValueError('online_hashes.json 包含不支持的包名')
        if name in assets:
            continue
        if (not isinstance(entry, dict) or type(entry.get('size')) is not int
                or not 0 < entry['size'] <= 300 * 1024 * 1024
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{64}', entry['sha256'])):
            raise ValueError('online_hashes.json 中 {} 缺少有效的大小和 SHA256'.format(name))
        assets[name] = {'size': entry['size'], 'sha256': entry['sha256'].lower()}
    return assets


def collect_all(version):
    assets = merge_online_hashes(collect_packages(version))
    if not assets:
        raise ValueError('没有可用的安装包：请放入本地安装包，或在 nps/online_hashes.json 中登记官方包的固定大小和 SHA256')
    return assets


def main():
    tree = ast.parse((PLUGIN / 'nps_main.py').read_text(encoding='utf-8'))
    identity = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == 'nps_main':
            for node in cls.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in ('version', 'repository'):
                            identity[target.id] = ast.literal_eval(node.value)
    version = identity.get('version')
    if not isinstance(version, str) or identity.get('repository') != 'ehang-io/nps':
        raise ValueError('无法读取 ehang-io/nps 后端适配版本')
    metadata = json.loads((PLUGIN / 'info.json').read_text(encoding='utf-8'))
    if metadata['name'] != 'nps':
        raise ValueError('插件名称不正确')
    assets = collect_all(version)
    manifest = {'repository': identity['repository'], 'version': version, 'packages': assets}
    (PLUGIN / 'packages.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')
    build_icon()
    local_names = [name for name in assets if (PLUGIN / name).is_file() and not (PLUGIN / name).is_symlink()]
    names = list(FILES) + ['packages.json'] + local_names
    target = ROOT / 'nps.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as package:
        for name in names:
            info = zipfile.ZipInfo('nps/' + name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED if name in local_names else zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name == 'install.sh' else 0o100644) << 16
            if name in local_names:
                with (PLUGIN / name).open('rb') as source, package.open(info, 'w') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            else:
                data = (PLUGIN / name).read_bytes()
                if name != 'icon.png':
                    data = data.replace(b'\r\n', b'\n')
                package.writestr(info, data)
    with zipfile.ZipFile(target) as package:
        if package.testzip() is not None or set(package.namelist()) != {'nps/' + name for name in names}:
            raise ValueError('插件 ZIP 校验失败')
        for name in local_names:
            asset = assets[name]
            digest = hashlib.sha256()
            with package.open('nps/' + name) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            if package.getinfo('nps/' + name).file_size != asset['size'] or digest.hexdigest() != asset['sha256']:
                raise ValueError('ZIP 内安装包与原始本地包不一致：' + name)
    print('已生成本地/在线安装版：' + str(target))
    print('包内文件：' + '、'.join(names))
    print('已固定哈希：本地包 {} 个，在线包 {} 个；适配 {} {}'.format(
        len(local_names), len(assets) - len(local_names), identity['repository'], version))


if __name__ == '__main__':
    main()
