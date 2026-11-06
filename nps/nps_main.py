#!/usr/bin/python
# coding: utf-8
# 宝塔 NPS 管理器，适配 ehang-io/nps v0.26.10。
import configparser
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import PurePosixPath

BASE_PATH = '/www/server/panel'
if os.path.isdir(os.path.join(BASE_PATH, 'class')):
    sys.path.insert(0, os.path.join(BASE_PATH, 'class'))
import public


class nps_main:
    version = 'v0.26.10'
    repository = 'ehang-io/nps'
    release_base = 'https://github.com/ehang-io/nps/releases/download'
    max_package_size = 300 * 1024 * 1024
    canonical_config_dir = '/etc/nps'
    package_dir = os.path.dirname(os.path.abspath(__file__))
    roots = {'nps': '/usr/local/bt-nps', 'npc': '/usr/local/bt-npc'}
    unit_dir = '/etc/systemd/system'
    backup_dir = '/www/backup/nps'
    lock_dir = '/www/server/panel/plugin/nps/.locks'
    port_fields = ('bridge_port', 'http_proxy_port', 'https_proxy_port', 'web_port', 'p2p_port')
    server_fields = port_fields + ('bridge_type', 'bridge_ip', 'web_ip', 'web_username',
                                   'web_password', 'public_vkey', 'log_level')
    client_fields = ('server_addr', 'vkey', 'conn_type', 'auto_reconnection')
    unit_marker = '# bt-nps-plugin\n'

    def _arg(self, get, name, default=None):
        if isinstance(get, dict):
            return get.get(name, default)
        return getattr(get, name, default)

    def _role(self, get):
        role = self._arg(get, 'role', 'nps')
        if role not in self.roots:
            raise ValueError('服务类型只能是 nps 或 npc')
        return role

    def _reply(self, action):
        try:
            return action()
        except (ValueError, OSError, RuntimeError, configparser.Error) as ex:
            return public.returnMsg(False, str(ex))
        except Exception:
            return public.returnMsg(False, '操作失败，请查看宝塔面板错误日志及服务日志')

    def _root(self, role):
        return self.roots[role]

    def _config_path(self, role):
        return os.path.join(self._root(role), 'conf', role + '.conf')

    def _log_path(self, role):
        return os.path.join(self._root(role), 'logs', role + '.log')

    def _unit(self, role):
        return 'bt-' + role + '.service'

    def _unit_path(self, role):
        return os.path.join(self.unit_dir, self._unit(role))

    def _read(self, path):
        with open(path, 'r', encoding='utf-8-sig') as handle:
            return handle.read()

    def _write(self, path, text, mode=0o600):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.bt-nps-', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _manifest(self, role):
        path = os.path.join(self._root(role), '.bt-plugin.json')
        try:
            data = json.loads(self._read(path))
            if (data.get('plugin') == 'nps' and data.get('role') == role
                    and data.get('repository') == self.repository and data.get('version') == self.version):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def _require_owned(self, role):
        if os.path.islink(self._root(role)) or not self._manifest(role):
            raise ValueError('该目录不是本插件管理的 ehang-io/nps v0.26.10 实例，已拒绝修改')

    def _require_config_location(self, role):
        # 旧版会优先读取 /etc/nps，不能通过启动参数指定其他配置目录。
        if role == 'nps' and os.path.lexists(self.canonical_config_dir):
            raise ValueError('检测到 {}；ehang-io/nps 会优先读取该目录。请先人工确认并处理此目录，本插件不会覆盖或删除它'.format(self.canonical_config_dir))

    def _require_systemd(self):
        if platform.system() != 'Linux' or not os.path.isdir('/run/systemd/system'):
            raise ValueError('本插件需要使用 systemd 的 Linux 宝塔面板')
        if not shutil.which('systemctl'):
            raise ValueError('未找到 systemctl，无法管理服务')

    def _run(self, args, check=True):
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError('服务命令执行超时，请检查 systemd 状态')
        if check and result.returncode:
            raise RuntimeError((result.stderr or result.stdout or '服务命令执行失败').strip()[:1500])
        return result

    def _state(self, role):
        result = self._run(['systemctl', 'is-active', self._unit(role)], check=False)
        return result.stdout.strip() or 'unknown'

    @contextmanager
    def _locked(self, role):
        import fcntl
        os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(self.lock_dir, role + '.lock'), 'a') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('该服务正在执行其他操作，请稍后重试')
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _revision(self, text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def _parse(self, role, text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > 1024 * 1024:
            raise ValueError('配置内容无效或超过 1 MB')
        if '\x00' in text:
            raise ValueError('配置中不能包含空字符')
        parser = configparser.ConfigParser(interpolation=None, delimiters=('=',),
                                           empty_lines_in_values=False)
        parser.optionxform = str
        parser.read_string(('[nps]\n' if role == 'nps' else '') + text)
        section = 'nps' if role == 'nps' else 'common'
        if not parser.has_section(section) or parser.defaults():
            raise ValueError('服务端使用无分节配置；客户端必须包含 [common]，不支持 DEFAULT 分节')
        if role == 'nps' and parser.sections() != ['nps']:
            raise ValueError('nps.conf 不应包含 INI 分节')
        for name in parser.sections():
            for value in parser[name].values():
                if '\n' in value:
                    raise ValueError('配置项必须为单行，请使用独立的 key=value 格式')
        return dict(parser[section])

    def _port(self, value, name, allow_zero=True):
        value = str(value)
        if not re.fullmatch(r'[0-9]{1,5}', value):
            raise ValueError(name + ' 必须为整数端口')
        port = int(value)
        if not (0 if allow_zero else 1) <= port <= 65535:
            raise ValueError(name + ' 超出允许的端口范围')
        return port

    def _secret(self, value, name, minimum=1, optional=False):
        if not value and optional:
            return
        if not minimum <= len(value) <= 256 or re.search(r'[\s#;\"\'\\]', value):
            raise ValueError(name + ' 长度不正确，或含空白、引号、反斜线、#、; 等配置特殊字符')

    def _validate(self, role, text, ready=False):
        config = self._parse(role, text)
        if role == 'nps':
            for name in self.port_fields:
                value = config.get(name, '')
                if name in ('http_proxy_port', 'https_proxy_port', 'p2p_port') and value == '':
                    continue
                self._port(value, name, name in ('http_proxy_port', 'https_proxy_port'))
            if config.get('p2p_port') and int(config['p2p_port']) > 65533:
                raise ValueError('P2P 连续使用 3 个 UDP 端口，起始端口不能大于 65533')
            if config.get('bridge_type') not in ('tcp', 'kcp'):
                raise ValueError('ehang-io/nps 的连接协议只能为 tcp 或 kcp')
            if int(config['web_port']) == int(config['bridge_port']):
                raise ValueError('Web 管理端口不能与连接端口相同，避免端口复用绕过 Web 监听地址限制')
            enabled = {'bridge_port': int(config['bridge_port']), 'web_port': int(config['web_port'])}
            for name in ('http_proxy_port', 'https_proxy_port'):
                if int(config.get(name, '0')):
                    enabled[name] = int(config[name])
            used = {}
            for name, port in enabled.items():
                if port in used:
                    raise ValueError('{} 与 {} 不能使用相同端口 {}'.format(used[port], name, port))
                used[port] = name
            for name in ('bridge_ip', 'web_ip'):
                try:
                    ipaddress.ip_address(config.get(name, ''))
                except ValueError:
                    raise ValueError(name + ' 必须为有效的 IPv4 或 IPv6 地址')
            if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', config.get('web_username', '')):
                raise ValueError('Web 用户名只能包含字母、数字、下划线、点、@ 和短横线')
            self._secret(config.get('web_password', ''), 'Web 密码', minimum=12)
            self._secret(config.get('public_vkey', ''), '公共 vkey', minimum=8, optional=True)
            if config.get('log_level', '6') not in tuple(str(level) for level in range(8)):
                raise ValueError('ehang-io/nps 的日志级别必须为 0–7 的数字')
            if config.get('log_path') != self._log_path(role):
                raise ValueError('请保留插件专用的 log_path，以便安全读取日志')
        else:
            address = config.get('server_addr', '')
            if address:
                match = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.-]+):([0-9]+)', address)
                if not match:
                    raise ValueError('服务器地址格式应为 域名:端口、IPv4:端口 或 [IPv6]:端口')
                self._port(match.group(2), '服务器端口', False)
            if ready and (not address or not config.get('vkey')):
                raise ValueError('请先配置服务器地址和客户端 vkey，再启动 npc')
            self._secret(config.get('vkey', ''), '客户端 vkey', optional=not ready)
            if config.get('conn_type') not in ('tcp', 'kcp'):
                raise ValueError('ehang-io/nps 的客户端连接协议只能为 tcp 或 kcp')
            if config.get('auto_reconnection', 'true') not in ('true', 'false'):
                raise ValueError('自动重连只能为 true 或 false')
        return config

    def _patch_config(self, role, text, changes):
        # 仅替换目标分节中的已知字段，保留注释、未知配置与客户端隧道。
        pending = dict(changes)
        result = []
        section = 'nps' if role == 'nps' else ''
        target = 'nps' if role == 'nps' else 'common'
        for line in text.splitlines():
            header = re.fullmatch(r'\s*\[([^]]+)\]\s*', line)
            if header:
                if section == target:
                    result.extend('{}={}'.format(k, v) for k, v in pending.items())
                    pending.clear()
                section = header.group(1)
            match = re.match(r'^\s*([^#;=\s]+)\s*=', line)
            if section == target and match and match.group(1) in pending:
                key = match.group(1)
                line = '{}={}'.format(key, pending.pop(key))
            result.append(line)
        if pending:
            if section != target:
                raise ValueError('未找到可更新的配置分节')
            result.extend('{}={}'.format(k, v) for k, v in pending.items())
        return '\n'.join(result) + '\n'

    def _save(self, role, text, revision):
        self._require_owned(role)
        current = self._read(self._config_path(role))
        if revision != self._revision(current):
            raise ValueError('配置已被其他操作修改，请刷新后重新编辑')
        self._validate(role, text)
        self._write(self._config_path(role) + '.bak', current)
        self._write(self._config_path(role), text)
        return public.returnMsg(True, '配置已保存并备份；请重启服务使配置生效')

    def get_config(self, get):
        def action():
            role = self._role(get)
            self._require_owned(role)
            text = self._read(self._config_path(role))
            return {'status': True, 'data': self._parse(role, text), 'revision': self._revision(text)}
        return self._reply(action)

    def set_config(self, get):
        def action():
            role = self._role(get)
            changes = json.loads(self._arg(get, 'config', '{}'))
            allowed = self.server_fields if role == 'nps' else self.client_fields
            if not isinstance(changes, dict) or any(k not in allowed for k in changes):
                raise ValueError('包含不允许修改的配置字段')
            for value in changes.values():
                if not isinstance(value, str) or any(c in value for c in ('\r', '\n', '\x00')):
                    raise ValueError('配置值必须为单行字符串')
            with self._locked(role):
                self._require_owned(role)
                current = self._read(self._config_path(role))
                return self._save(role, self._patch_config(role, current, changes), self._arg(get, 'revision'))
        return self._reply(action)

    def get_config_file(self, get):
        def action():
            role = self._role(get)
            self._require_owned(role)
            text = self._read(self._config_path(role))
            return {'status': True, 'data': text, 'path': self._config_path(role), 'revision': self._revision(text)}
        return self._reply(action)

    def set_config_file(self, get):
        def action():
            role = self._role(get)
            with self._locked(role):
                return self._save(role, self._arg(get, 'content'), self._arg(get, 'revision'))
        return self._reply(action)

    def get_info(self, get):
        def action():
            role = self._role(get)
            manifest = self._manifest(role)
            installed = bool(manifest and os.path.isfile(os.path.join(self._root(role), role))
                             and os.path.isfile(self._unit_path(role)))
            data = {'installed': installed, 'running': False, 'state': 'not-installed',
                    'version': manifest.get('version', ''), 'target_version': self.version,
                    'config_path': self._config_path(role), 'log_path': self._log_path(role),
                    'unit': self._unit(role), 'dashboard': '', 'local_dashboard': '',
                    'release_url': 'https://github.com/' + self.repository + '/releases/tag/' + self.version,
                    'package_available': False, 'package_path': '', 'package_error': '',
                    'repository': self.repository, 'environment_error': ''}
            try:
                self._require_config_location(role)
            except ValueError as ex:
                data['environment_error'] = str(ex)
            try:
                status = self._source_status(role)
                data.update(status)
                data['package_available'] = status['local_available']
                data['package_error'] = status['local_error']
            except (ValueError, OSError) as ex:
                data['package_error'] = str(ex)
            if installed:
                self._require_systemd()
                data['state'] = self._state(role)
                data['running'] = data['state'] == 'active'
                if role == 'nps':
                    try:
                        config = self._parse(role, self._read(self._config_path(role)))
                        host = config.get('web_ip', '127.0.0.1')
                        local = ipaddress.ip_address(host).is_loopback
                        if host in ('0.0.0.0', '::'):
                            host = public.get_server_ip()
                        host = '[' + host + ']' if ':' in host and not host.startswith('[') else host
                        scheme = 'https' if config.get('web_open_ssl') == 'true' else 'http'
                        port = self._port(config.get('web_port', '0'), 'web_port', False)
                        address = '{}://{}:{}{}'.format(scheme, host, port, config.get('web_base_url', ''))
                        data['local_dashboard' if local else 'dashboard'] = address
                    except (ValueError, OSError, configparser.Error):
                        data['config_error'] = '配置无法解析，请到“配置文件”页修正'
            return {'status': True, 'data': data}
        return self._reply(action)

    def get_logs(self, get):
        def action():
            role = self._role(get)
            self._require_owned(role)
            path = self._log_path(role)
            if not os.path.isfile(path):
                return {'status': True, 'data': '尚未生成日志；启动失败时请使用 journalctl -u ' + self._unit(role) + ' -n 100 --no-pager', 'path': path}
            with open(path, 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 128 * 1024))
                text = handle.read().decode('utf-8', errors='replace')
            return {'status': True, 'data': text, 'path': path}
        return self._reply(action)

    def service_admin(self, get):
        def action():
            role = self._role(get)
            command = self._arg(get, 'status')
            if command not in ('start', 'stop', 'restart'):
                raise ValueError('不支持的服务操作')
            self._require_systemd()
            with self._locked(role):
                self._require_owned(role)
                if not os.path.isfile(self._unit_path(role)):
                    raise ValueError('服务文件不存在，请先安装或重装服务')
                if not self._read(self._unit_path(role)).startswith(self.unit_marker):
                    raise ValueError('同名 systemd 服务不属于本插件，已拒绝操作')
                if command != 'stop':
                    self._require_config_location(role)
                    self._validate(role, self._read(self._config_path(role)), ready=True)
                    self._run(['systemctl', 'reset-failed', self._unit(role)], check=False)
                self._run(['systemctl', command, self._unit(role)])
                if command != 'stop':
                    time.sleep(2)
                    if self._state(role) != 'active':
                        raise RuntimeError('服务未保持运行，请查看日志或 journalctl -u ' + self._unit(role))
                    self._run(['systemctl', 'enable', self._unit(role)])
                elif self._state(role) in ('active', 'activating', 'deactivating'):
                    raise RuntimeError('服务尚未停止，请稍后刷新状态')
                return public.returnMsg(True, '操作成功；运行状态仅表示进程状态，不代表客户端已连接')
        return self._reply(action)

    def _architecture(self):
        machine = platform.machine().lower()
        mapping = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}
        if machine not in mapping:
            raise ValueError('当前仅支持 Linux amd64 和 arm64')
        return mapping[machine]

    def _package_name(self, role):
        return 'linux_{}_{}.tar.gz'.format(self._architecture(), 'server' if role == 'nps' else 'client')

    def _package_manifest(self):
        index = os.path.join(self.package_dir, 'packages.json')
        if os.path.islink(index) or not os.path.isfile(index):
            raise ValueError('缺少安装包校验索引 packages.json，请运行 build_nps.py 重新打包')
        if os.path.getsize(index) > 64 * 1024:
            raise ValueError('安装包校验索引超过大小限制')
        try:
            manifest = json.loads(self._read(index))
        except (ValueError, UnicodeError):
            raise ValueError('安装包校验索引格式错误，请重新打包')
        if (not isinstance(manifest, dict) or manifest.get('version') != self.version
                or manifest.get('repository') != self.repository
                or not isinstance(manifest.get('packages'), dict)):
            raise ValueError('安装包索引来源或版本与插件不匹配，请使用 {} {} 的安装包重新打包'.format(self.repository, self.version))
        return manifest

    def _pinned_asset(self, name):
        asset = self._package_manifest()['packages'].get(name)
        if (not isinstance(asset, dict) or not isinstance(asset.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{64}', asset['sha256'])
                or type(asset.get('size')) is not int or not 0 < asset['size'] <= self.max_package_size):
            raise ValueError('安装包 {} 未固定有效的大小和 SHA256，请重新打包'.format(name))
        return {'name': name, 'size': asset['size'], 'sha256': asset['sha256'].lower()}

    def _local_asset(self, role):
        name = self._package_name(role)
        path = os.path.join(self.package_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError('缺少本地安装包 {}；可改用在线安装，或将 {} 安装包放入插件目录后重新打包'.format(path, self.version))
        asset = self._pinned_asset(name)
        if os.path.getsize(path) != asset['size']:
            raise ValueError('本地安装包大小校验失败，请重新上传完整的插件包')
        return path, asset

    def _online_asset(self, role):
        name = self._package_name(role)
        asset = self._pinned_asset(name)
        asset['url'] = '{}/{}/{}'.format(self.release_base, self.version, name)
        return asset

    def _source_status(self, role):
        status = {'local_available': False, 'local_error': '', 'online_available': False,
                  'online_error': '', 'package_path': os.path.join(self.package_dir, self._package_name(role)),
                  'online_url': ''}
        try:
            self._local_asset(role)
            status['local_available'] = True
        except (ValueError, OSError) as ex:
            status['local_error'] = str(ex)
        try:
            status['online_url'] = self._online_asset(role)['url']
            status['online_available'] = True
        except (ValueError, OSError) as ex:
            status['online_error'] = str(ex)
        return status

    def _copy_local_package(self, source, destination, asset):
        digest = hashlib.sha256()
        size = 0
        with open(source, 'rb') as package, open(destination, 'wb') as handle:
            while True:
                block = package.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > asset['size']:
                    raise ValueError('本地安装包超过固定大小限制')
                digest.update(block)
                handle.write(block)
        if size != asset['size'] or digest.hexdigest() != asset['sha256']:
            raise ValueError('本地安装包大小或 SHA256 校验失败，请重新上传完整的插件包')

    def _download_package(self, destination, asset):
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(asset['url'], headers={'User-Agent': 'BT-NPS-Plugin', 'Accept': 'application/octet-stream'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response, open(destination, 'wb') as handle:
                final = urllib.parse.urlsplit(response.geturl())
                host = (final.hostname or '').lower()
                if final.scheme != 'https' or not (host == 'github.com' or host.endswith('.githubusercontent.com')):
                    raise ValueError('在线安装包地址不是 GitHub 官方 HTTPS 地址，已中止')
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > asset['size']:
                        raise ValueError('在线安装包超过固定大小限制，已中止')
                    digest.update(block)
                    handle.write(block)
        except ValueError:
            raise
        except OSError as ex:
            reason = getattr(ex, 'reason', None) or ex
            raise RuntimeError('在线下载失败：{}；可改用本地安装包'.format(reason))
        if size != asset['size'] or digest.hexdigest() != asset['sha256']:
            raise ValueError('在线安装包大小或 SHA256 校验失败，已中止')

    def _prepare_package(self, role, destination, source='local'):
        if source == 'local':
            path, asset = self._local_asset(role)
            self._copy_local_package(path, destination, asset)
        elif source == 'online':
            asset = self._online_asset(role)
            self._download_package(destination, asset)
        else:
            raise ValueError('安装来源只能是 local 或 online')
        return asset

    def _extract(self, archive, destination):
        # 拒绝路径穿越、链接、设备节点和异常体积，不直接调用 extractall。
        total = 0
        with tarfile.open(archive, 'r:gz') as package:
            for count, item in enumerate(package):
                path = PurePosixPath(item.name)
                if path.is_absolute() or '..' in path.parts or '\\' in item.name or ':' in item.name:
                    raise ValueError('安装包包含不安全路径')
                if not (item.isfile() or item.isdir()) or count > 20000:
                    raise ValueError('安装包包含不支持的文件类型或过多文件')
                total += item.size
                if total > 600 * 1024 * 1024:
                    raise ValueError('安装包解压后超过大小限制')
                target = os.path.join(destination, *path.parts)
                if item.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with package.extractfile(item) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)

    def _initial_config(self, role, template):
        if role == 'npc':
            return '# 先在 NPS 管理后台添加客户端，再填写地址和该客户端的 vkey。\n[common]\nserver_addr=\nvkey=\nconn_type=tcp\nauto_reconnection=true\ncrypt=true\ncompress=true\n'
        changes = {'http_proxy_port': '18080', 'https_proxy_port': '18443', 'p2p_port': '18000'}
        changes.update({'bridge_type': 'tcp', 'bridge_port': '8024', 'bridge_ip': '0.0.0.0',
                        'runmode': 'pro', 'web_ip': '127.0.0.1', 'web_port': '18081', 'web_host': '',
                        'web_username': 'admin', 'web_password': secrets.token_urlsafe(24),
                        'public_vkey': '', 'auth_key': '', 'auth_crypt_key': '',
                        'allow_user_register': 'false', 'allow_user_login': 'false',
                        'web_open_ssl': 'false', 'flow_store_interval': '1',
                        'log_level': '6', 'log_path': self._log_path(role)})
        return self._patch_config(role, template, changes)

    def _service_text(self, role):
        root = self._root(role)
        executable = root + '/' + role
        if role == 'nps':
            args = 'service'
        else:
            args = '-config=' + self._config_path(role) + ' -debug=false -log=file -log_level=6 -log_path=' + self._log_path(role)
        condition = 'ConditionPathExists=!{}\n'.format(self.canonical_config_dir) if role == 'nps' else ''
        return self.unit_marker + '''[Unit]
Description=BT {role} proxy service
Wants=network-online.target
After=network-online.target
{condition}StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={executable} {args}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
'''.format(role=role, root=root, executable=executable, args=args, condition=condition)

    def _backup(self, role):
        os.makedirs(self.backup_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.backup_dir, '{}-{}-{}.tar.gz'.format(role, time.strftime('%Y%m%d-%H%M%S'), secrets.token_hex(4)))
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
            archive.add(self._root(role), arcname='bt-' + role)
        return path

    def _install(self, role, reinstall=False, source='local'):
        if source not in ('local', 'online'):
            raise ValueError('安装来源只能是 local 或 online')
        self._require_systemd()
        self._require_config_location(role)
        root = self._root(role)
        existed = os.path.exists(root)
        if existed:
            self._require_owned(role)
            for directory, directories, files in os.walk(root):
                if any(os.path.islink(os.path.join(directory, name)) for name in directories + files):
                    raise ValueError('安装目录包含符号链接，请先人工确认后再重装')
            if not reinstall and os.path.isfile(os.path.join(root, role)) and os.path.isfile(self._unit_path(role)):
                raise ValueError('已经安装，请使用“备份并重装”')
        unit_path = self._unit_path(role)
        old_unit = self._read(unit_path) if os.path.isfile(unit_path) else None
        if old_unit is not None and not old_unit.startswith(self.unit_marker):
            raise ValueError('同名 systemd 服务不属于本插件，已拒绝覆盖')
        backup = ''
        # 本地包复制、校验和解压在停止旧服务之前完成，包损坏不影响原服务。
        with tempfile.TemporaryDirectory(prefix='.bt-nps-install-', dir=os.path.dirname(root)) as temporary:
            archive = os.path.join(temporary, 'package.tar.gz')
            asset = self._prepare_package(role, archive, source)
            stage = os.path.join(temporary, 'stage')
            os.mkdir(stage, 0o700)
            self._extract(archive, stage)
            binary = os.path.join(stage, role)
            config = os.path.join(stage, 'conf', role + '.conf')
            if not os.path.isfile(binary) or not os.path.isfile(config):
                raise ValueError('安装包缺少程序或配置文件')
            if role == 'nps' and not all(os.path.isdir(os.path.join(stage, 'web', name)) for name in ('views', 'static')):
                raise ValueError('服务端安装包缺少 Web 管理界面资源')
            was_running = old_unit is not None and self._state(role) in ('active', 'activating')
            was_enabled = old_unit is not None and self._run(['systemctl', 'is-enabled', self._unit(role)], check=False).returncode == 0
            previous = root + '.previous-' + secrets.token_hex(4)
            moved_old = False
            moved_new = False
            try:
                if old_unit is not None:
                    self._run(['systemctl', 'stop', self._unit(role)])
                if existed:
                    backup = self._backup(role)
                    # 保留全部旧数据，只替换可执行文件及上游 Web 静态资源。
                    for name in os.listdir(root):
                        if name in (role, 'web', '.bt-plugin.json'):
                            continue
                        old_item, new_item = os.path.join(root, name), os.path.join(stage, name)
                        if os.path.isdir(new_item):
                            shutil.rmtree(new_item)
                        elif os.path.lexists(new_item):
                            os.unlink(new_item)
                        if os.path.islink(old_item):
                            raise ValueError('安装目录包含符号链接，请先人工确认后再重装')
                        if os.path.isdir(old_item):
                            shutil.copytree(old_item, new_item, symlinks=True)
                        else:
                            shutil.copy2(old_item, new_item)
                else:
                    self._write(config, self._initial_config(role, self._read(config)))
                self._validate(role, self._read(config))
                os.chmod(config, 0o600)
                os.chmod(binary, 0o755)
                os.makedirs(os.path.join(stage, 'logs'), mode=0o700, exist_ok=True)
                self._write(os.path.join(stage, '.bt-plugin.json'), json.dumps({'plugin': 'nps', 'role': role, 'version': self.version,
                                                                              'repository': self.repository, 'source': source,
                                                                              'package': asset['name'], 'sha256': asset['sha256']}))
                if existed:
                    os.rename(root, previous)
                    moved_old = True
                os.rename(stage, root)
                moved_new = True
                self._write(unit_path, self._service_text(role), 0o644)
                self._run(['systemctl', 'daemon-reload'])
                if role == 'nps' or was_enabled:
                    self._run(['systemctl', 'enable', self._unit(role)])
            except Exception as error:
                try:
                    if not was_enabled:
                        self._run(['systemctl', 'disable', self._unit(role)], check=False)
                    if moved_new:
                        shutil.rmtree(root)
                    if moved_old:
                        os.rename(previous, root)
                    if old_unit is not None:
                        self._write(unit_path, old_unit, 0o644)
                    elif os.path.exists(unit_path):
                        os.unlink(unit_path)
                    self._run(['systemctl', 'daemon-reload'])
                    if was_running:
                        self._run(['systemctl', 'start', self._unit(role)])
                except Exception:
                    raise RuntimeError('安装失败且自动恢复未完成；请检查 {} 及备份 {}，不要删除这些数据'.format(previous, backup or '未生成')) from error
                raise
            if moved_old:
                shutil.rmtree(previous)
        message = '安装完成，请检查配置后手动启动服务；启动成功后自动设置开机启动'
        if backup:
            message = '重装完成，配置和数据已保留，服务已停止，请手动启动。备份：' + backup
        return public.returnMsg(True, '{}（来源：{}）'.format(message, '在线下载' if source == 'online' else '本地安装包'))

    def install(self, get):
        def action():
            role = self._role(get)
            source = self._arg(get, 'source', 'local')
            with self._locked(role):
                return self._install(role, False, source)
        return self._reply(action)

    def reinstall(self, get):
        def action():
            role = self._role(get)
            source = self._arg(get, 'source', 'local')
            with self._locked(role):
                self._require_owned(role)
                return self._install(role, True, source)
        return self._reply(action)
