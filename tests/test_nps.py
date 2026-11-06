# coding: utf-8
# 使用模拟面板与 systemd，测试不会安装程序或修改系统服务。
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
public = types.ModuleType('public')
public.returnMsg = lambda status, msg: {'status': status, 'msg': msg}
public.get_server_ip = lambda: '203.0.113.8'
sys.modules['public'] = public
spec = importlib.util.spec_from_file_location('nps_plugin', ROOT / 'nps' / 'nps_main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
build_spec = importlib.util.spec_from_file_location('build_nps', ROOT / 'build_nps.py')
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)


def archive_bytes(entries, special=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for name, content in entries.items():
            content = content.encode('utf-8') if isinstance(content, str) else content
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        if special:
            archive.addfile(special)
    return data.getvalue()


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.nps-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.plugin = module.nps_main()
        self.plugin.roots = {role: str(self.base / ('bt-' + role)) for role in ('nps', 'npc')}
        self.plugin.unit_dir = str(self.base / 'units')
        self.plugin.backup_dir = str(self.base / 'backups')
        self.plugin.package_dir = str(self.base / 'packages')
        self.plugin.canonical_config_dir = str(self.base / 'etc-nps')
        self.plugin._require_systemd = lambda: None
        self.plugin._locked = lambda role: nullcontext()
        self.commands = []
        self.states = {'bt-nps.service': 'inactive', 'bt-npc.service': 'inactive'}
        self.enabled = set()
        self.fail_enable = False
        self.plugin._run = self.run_command
        self.sleep = patch.object(module.time, 'sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.network = patch('socket.create_connection', side_effect=AssertionError('离线安装不得发起网络连接'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_command(self, args, check=True):
        self.commands.append(args)
        action = args[1]
        unit = args[2] if len(args) > 2 else ''
        code, output = 0, ''
        if action == 'is-active':
            output = self.states.get(unit, 'inactive')
            code = 0 if output == 'active' else 3
        elif action == 'is-enabled':
            code = 0 if unit in self.enabled else 1
        elif action in ('start', 'restart'):
            self.states[unit] = 'active'
        elif action == 'stop':
            self.states[unit] = 'inactive'
        elif action == 'enable':
            if self.fail_enable:
                raise RuntimeError('模拟启用服务失败')
            self.enabled.add(unit)
        elif action == 'disable':
            self.enabled.discard(unit)
        return subprocess.CompletedProcess(args, code, output, '')

    def provision(self, role='nps'):
        root = Path(self.plugin._root(role))
        root.mkdir()
        text = self.plugin._initial_config(role, '# 保留注释\nappname=nps\n')
        self.plugin._write(self.plugin._config_path(role), text)
        self.plugin._write(str(root / '.bt-plugin.json'), json.dumps({'plugin': 'nps', 'role': role, 'version': self.plugin.version, 'repository': self.plugin.repository}))
        (root / role).write_text('模拟二进制', encoding='utf-8')
        self.plugin._write(self.plugin._unit_path(role), self.plugin._service_text(role))
        return text

    def mock_package(self, role='nps'):
        entries = {role: '模拟新版程序', 'conf/' + role + '.conf': 'appname=nps\n'}
        if role == 'nps':
            entries.update({'web/views/index.html': '页面', 'web/static/style.css': '样式'})
        return self.put_package(role, archive_bytes(entries))

    def put_package(self, role, content):
        directory = Path(self.plugin.package_dir)
        directory.mkdir(exist_ok=True)
        path = directory / self.plugin._package_name(role)
        path.write_bytes(content)
        index = directory / 'packages.json'
        manifest = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {'packages': {}}
        manifest.update({'version': self.plugin.version, 'repository': self.plugin.repository})
        manifest['packages'][path.name] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        index.write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def test_server_defaults(self):
        text = self.provision()
        config = self.plugin._validate('nps', text)
        self.assertEqual(config['bridge_port'], '8024')
        self.assertEqual(config['bridge_type'], 'tcp')
        self.assertEqual(config['log_level'], '6')
        self.assertEqual(config['p2p_port'], '')
        self.assertEqual(config['runmode'], 'pro')
        self.assertEqual(config['web_ip'], '127.0.0.1')
        self.assertEqual(config['web_port'], '18081')
        self.assertEqual(config['http_proxy_port'], '0')
        self.assertEqual(config['public_vkey'], '')
        self.assertGreaterEqual(len(config['web_password']), 24)
        other = self.plugin._initial_config('nps', '')
        self.assertNotEqual(config['web_password'], self.plugin._parse('nps', other)['web_password'])

    def test_numeric_password_preserved_and_backup(self):
        old = self.provision()
        revision = self.plugin._revision(old)
        res = self.plugin.set_config({'role': 'nps', 'revision': revision, 'config': json.dumps({'web_password': '0001234567890123', 'web_port': '18082'})})
        self.assertTrue(res['status'], res)
        current = self.plugin.get_config({'role': 'nps'})['data']
        self.assertEqual(current['web_password'], '0001234567890123')
        self.assertEqual(current['web_port'], '18082')
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps') + '.bak'), old)

    def test_client_sections_preserved(self):
        self.provision('npc')
        text = '# 客户端注释\n[common]\nserver_addr=old.example:8024\nvkey=abc12345\nconn_type=tcp\ncustom=keep\n\n[ssh]\nmode=tcp\nserver_port=10022\ntarget_addr=127.0.0.1:22\n'
        self.plugin._write(self.plugin._config_path('npc'), text)
        res = self.plugin.set_config({'role': 'npc', 'revision': self.plugin._revision(text), 'config': json.dumps({'server_addr': '[2001:db8::1]:8024', 'auto_reconnection': 'true'})})
        self.assertTrue(res['status'], res)
        output = self.plugin._read(self.plugin._config_path('npc'))
        self.assertIn('custom=keep', output)
        self.assertIn('[ssh]\nmode=tcp\nserver_port=10022', output)
        self.assertLess(output.index('auto_reconnection=true'), output.index('[ssh]'))

    def test_reject_stale_revision(self):
        old = self.provision()
        result = self.plugin.set_config({'role': 'nps', 'revision': 'stale', 'config': '{"web_port":"18082"}'})
        self.assertFalse(result['status'])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), old)

    def test_reject_invalid_fields_and_injection(self):
        text = self.provision()
        for data in ({'web_port': '65536'}, {'p2p_port': '65534'}, {'web_port': '0'}, {'web_ip': 'bad'},
                     {'web_password': 'short'}, {'web_username': 'admin\nweb_port=80'}, {'unknown': 'x'},
                     {'web_port': 80}, {'bridge_port': '0'}, {'bridge_type': 'invalid'}, {'p2p_port': '0'},
                     {'log_level': 'info'}, {'log_level': '8'}, {'web_port': '8024'}):
            with self.subTest(data=data):
                result = self.plugin.set_config({'role': 'nps', 'revision': self.plugin._revision(text), 'config': json.dumps(data)})
                self.assertFalse(result['status'], result)
                self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), text)

    def test_numeric_log_levels_and_p2p_blank(self):
        text = self.provision()
        for level in range(8):
            with self.subTest(level=level):
                config = self.plugin._validate('nps', self.plugin._patch_config('nps', text, {'log_level': str(level)}))
                self.assertEqual(config['log_level'], str(level))
        for port in ('', '6000', '65533'):
            config = self.plugin._validate('nps', self.plugin._patch_config('nps', text, {'p2p_port': port}))
            self.assertEqual(config['p2p_port'], port)

    def test_tcp_kcp_are_the_only_connection_protocols(self):
        server = self.provision()
        client = self.provision('npc')
        for protocol in ('tcp', 'kcp'):
            self.plugin._validate('nps', self.plugin._patch_config('nps', server, {'bridge_type': protocol}))
            self.plugin._validate('npc', self.plugin._patch_config('npc', client, {'conn_type': protocol}))
        for protocol in ('http', 'udp', ''):
            with self.assertRaises(ValueError):
                self.plugin._validate('npc', self.plugin._patch_config('npc', client, {'conn_type': protocol}))

    def test_client_cannot_start_without_credentials(self):
        self.provision('npc')
        result = self.plugin.service_admin({'role': 'npc', 'status': 'start'})
        self.assertFalse(result['status'])
        self.assertEqual(self.commands, [])

    def test_can_stop_invalid_config(self):
        self.provision()
        self.plugin._write(self.plugin._config_path('nps'), 'broken')
        result = self.plugin.service_admin({'role': 'nps', 'status': 'stop'})
        self.assertTrue(result['status'], result)
        self.assertIn(['systemctl', 'stop', 'bt-nps.service'], self.commands)

    def test_reject_invalid_role_and_command(self):
        self.assertFalse(self.plugin.get_info({'role': '../../etc'})['status'])
        self.assertFalse(self.plugin.service_admin({'role': 'nps', 'status': 'start;whoami'})['status'])
        self.assertEqual(self.commands, [])

    def test_get_info_local_public_and_ipv6(self):
        text = self.provision()
        self.assertEqual(self.plugin.get_info({'role': 'nps'})['data']['local_dashboard'], 'http://127.0.0.1:18081')
        for address, expected in [('0.0.0.0', 'http://203.0.113.8:18081'), ('2001:db8::1', 'http://[2001:db8::1]:18081')]:
            self.plugin._write(self.plugin._config_path('nps'), text.replace('web_ip=127.0.0.1', 'web_ip=' + address))
            self.assertEqual(self.plugin.get_info({'role': 'nps'})['data']['dashboard'], expected)

    def test_raw_config_rejects_broken_format(self):
        text = self.provision()
        for content in ('broken', text + '\nweb_port=80\n', text + '\n[bad]\nport=80\n'):
            result = self.plugin.set_config_file({'role': 'nps', 'revision': self.plugin._revision(text), 'content': content})
            self.assertFalse(result['status'])
            self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), text)

    def test_broken_raw_config_remains_readable(self):
        self.provision()
        self.plugin._write(self.plugin._config_path('nps'), 'broken')
        result = self.plugin.get_config_file({'role': 'nps'})
        self.assertTrue(result['status'])
        self.assertEqual(result['data'], 'broken')

    def test_log_tail_is_bounded(self):
        self.provision()
        path = Path(self.plugin._log_path('nps'))
        path.parent.mkdir()
        path.write_bytes(b'A' * 200000 + b'END')
        result = self.plugin.get_logs({'role': 'nps'})
        self.assertTrue(result['data'].endswith('END'))
        self.assertEqual(len(result['data']), 128 * 1024)

    def test_architecture_mapping(self):
        for name, expected in [('aarch64', 'arm64'), ('arm64', 'arm64'), ('x86_64', 'amd64')]:
            with patch.object(module.platform, 'machine', return_value=name):
                self.assertEqual(self.plugin._architecture(), expected)
        with patch.object(module.platform, 'machine', return_value='mips'):
            with self.assertRaises(ValueError):
                self.plugin._architecture()

    def test_archive_rejects_traversal_and_links(self):
        archive = self.base / 'bad.tar.gz'
        target = self.base / 'stage'
        target.mkdir()
        for name in ('../escape', '/absolute', 'dir/../../escape', 'C:/escape', 'dir\\escape'):
            archive.write_bytes(archive_bytes({name: 'x'}))
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.plugin._extract(str(archive), str(target))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            item = tarfile.TarInfo('bad-link')
            item.type = kind
            item.linkname = '../escape'
            archive.write_bytes(archive_bytes({}, item))
            with self.assertRaises(ValueError):
                self.plugin._extract(str(archive), str(target))
        self.assertFalse((self.base / 'escape').exists())

    def test_local_package_copy_and_digest_verification(self):
        content = b'archive-data\r\n\x00\xff'
        source = self.put_package('nps', content)
        destination = str(self.base / 'package.tar.gz')
        self.plugin._prepare_package('nps', destination)
        self.assertEqual(Path(destination).read_bytes(), content)
        self.assertEqual(source.read_bytes(), content)
        source.write_bytes(b'X' * len(content))
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.plugin._prepare_package('nps', destination)

    def test_local_package_wrong_size_rejected(self):
        path = self.put_package('nps', b'original')
        path.write_bytes(b'truncated')
        with self.assertRaisesRegex(ValueError, '大小校验失败'):
            self.plugin._prepare_package('nps', str(self.base / 'package.tar.gz'))

    def test_local_package_requires_index(self):
        self.put_package('nps', b'original')
        os.unlink(Path(self.plugin.package_dir) / 'packages.json')
        with self.assertRaisesRegex(ValueError, 'packages.json'):
            self.plugin._prepare_package('nps', str(self.base / 'package.tar.gz'))

    def test_local_package_rejects_invalid_index(self):
        path = self.put_package('nps', b'original')
        index = Path(self.plugin.package_dir) / 'packages.json'
        invalid = ['broken', '[]', '{"version":"old"}', json.dumps({'version': self.plugin.version, 'packages': {}})]
        for data in invalid:
            with self.subTest(data=data):
                index.write_text(data, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.plugin._prepare_package('nps', str(self.base / 'package.tar.gz'))
        self.put_package('nps', b'original')
        data = json.loads(index.read_text(encoding='utf-8'))
        data['packages'][path.name]['size'] = 301 * 1024 * 1024
        index.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.plugin._prepare_package('nps', str(self.base / 'package.tar.gz'))

    def test_missing_architecture_never_uses_other_package(self):
        with patch.object(self.plugin, '_architecture', return_value='amd64'):
            self.mock_package()
        with patch.object(self.plugin, '_architecture', return_value='arm64'):
            result = self.plugin.install({'role': 'nps'})
            self.assertFalse(result['status'])
            self.assertIn('linux_arm64_server.tar.gz', result['msg'])
        self.assertEqual(self.commands, [])

    def test_role_selects_separate_local_package(self):
        server = self.put_package('nps', b'server-package')
        client = self.put_package('npc', b'client-package')
        self.assertNotEqual(server.name, client.name)
        target = self.base / 'package.tar.gz'
        self.plugin._prepare_package('npc', str(target))
        self.assertEqual(target.read_bytes(), b'client-package')

    def test_package_status_does_not_block_running_service(self):
        self.provision()
        self.states['bt-nps.service'] = 'active'
        data = self.plugin.get_info({'role': 'nps'})['data']
        self.assertTrue(data['running'])
        self.assertFalse(data['package_available'])
        self.assertIn('缺少本地安装包', data['package_error'])
        self.mock_package()
        data = self.plugin.get_info({'role': 'nps'})['data']
        self.assertTrue(data['package_available'])
        self.assertEqual(data['package_error'], '')

    def test_local_package_rejects_symlink(self):
        path = self.put_package('nps', b'original')
        original = os.path.islink
        with patch.object(module.os.path, 'islink', side_effect=lambda name: str(name) == str(path) or original(name)):
            with self.assertRaises(ValueError):
                self.plugin._prepare_package('nps', str(self.base / 'package.tar.gz'))

    def test_installer_network_is_limited_to_online_source(self):
        source = (ROOT / 'nps' / 'nps_main.py').read_text(encoding='utf-8')
        self.assertNotIn('api.github.com', source)
        self.assertIn('https://github.com/ehang-io/nps/releases/download', source)
        self.assertIn('_pinned_asset', source)
        self.assertNotIn('_package_metadata', source)

    def pin_package(self, role, content, name=None):
        directory = Path(self.plugin.package_dir)
        directory.mkdir(exist_ok=True)
        index = directory / 'packages.json'
        manifest = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {'packages': {}}
        manifest.update({'version': self.plugin.version, 'repository': self.plugin.repository})
        name = name or self.plugin._package_name(role)
        manifest['packages'][name] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        index.write_text(json.dumps(manifest), encoding='utf-8')
        return name

    def server_archive(self):
        return archive_bytes({'nps': '在线程序', 'conf/nps.conf': 'appname=nps\n',
                              'web/views/index.html': '页面', 'web/static/main.css': '样式'})

    def test_online_install_uses_pinned_github_asset(self):
        content = self.server_archive()
        name = self.pin_package('nps', content)
        asset = self.plugin._online_asset('nps')
        self.assertEqual(asset['url'], 'https://github.com/ehang-io/nps/releases/download/v0.26.10/' + name)
        self.assertFalse(Path(self.plugin.package_dir, name).exists())
        response = FakeResponse(content, 'https://objects.githubusercontent.com/release/' + name)
        with patch.object(module.urllib.request, 'urlopen', return_value=response) as opener:
            result = self.plugin.install({'role': 'nps', 'source': 'online'})
        self.assertTrue(result['status'], result)
        self.assertIn('在线下载', result['msg'])
        self.assertEqual(opener.call_args[0][0].full_url, asset['url'])
        self.assertEqual(self.plugin._manifest('nps')['source'], 'online')
        self.assertEqual(self.plugin._manifest('nps')['sha256'], asset['sha256'])
        self.assertFalse(Path(self.plugin.package_dir, name).exists())

    def test_online_requires_pinned_hash(self):
        directory = Path(self.plugin.package_dir)
        directory.mkdir(exist_ok=True)
        manifest = {'repository': self.plugin.repository, 'version': self.plugin.version, 'packages': {}}
        (directory / 'packages.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '未固定'):
            self.plugin._online_asset('nps')
        info = self.plugin.get_info({'role': 'nps'})['data']
        self.assertFalse(info['online_available'])
        self.assertIn('未固定', info['online_error'])
        self.assertEqual(info['online_url'], '')

    def test_online_hash_mismatch_rejected_before_stop(self):
        old = self.provision()
        self.states['bt-nps.service'] = 'active'
        content = self.server_archive()
        self.pin_package('nps', content)
        tampered = b'X' * len(content)
        response = FakeResponse(tampered, 'https://objects.githubusercontent.com/x')
        with patch.object(module.urllib.request, 'urlopen', return_value=response):
            result = self.plugin.reinstall({'role': 'nps', 'source': 'online'})
        self.assertFalse(result['status'])
        self.assertIn('SHA256', result['msg'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), old)

    def test_online_non_github_host_rejected(self):
        self.pin_package('nps', self.server_archive())
        response = FakeResponse(b'data', 'https://evil.example.com/package.tar.gz')
        with patch.object(module.urllib.request, 'urlopen', return_value=response):
            result = self.plugin.install({'role': 'nps', 'source': 'online'})
        self.assertFalse(result['status'])
        self.assertIn('GitHub', result['msg'])
        self.assertEqual(self.commands, [])

    def test_online_network_error_is_friendly(self):
        self.pin_package('nps', self.server_archive())
        with patch.object(module.urllib.request, 'urlopen', side_effect=module.urllib.error.URLError('模拟超时')):
            result = self.plugin.install({'role': 'nps', 'source': 'online'})
        self.assertFalse(result['status'])
        self.assertIn('在线下载失败', result['msg'])
        self.assertIn('本地安装包', result['msg'])
        self.assertEqual(self.commands, [])

    def test_reject_unknown_install_source(self):
        self.mock_package()
        result = self.plugin.install({'role': 'nps', 'source': 'mirror'})
        self.assertFalse(result['status'])
        self.assertIn('local 或 online', result['msg'])
        self.assertEqual(self.commands, [])
        self.assertFalse(Path(self.plugin._root('nps')).exists())

    def test_new_install_creates_independent_unit(self):
        self.mock_package()
        result = self.plugin.install({'role': 'nps'})
        self.assertTrue(result['status'], result)
        self.assertTrue(Path(self.plugin._unit_path('nps')).exists())
        self.assertEqual(self.plugin._manifest('nps')['version'], self.plugin.version)
        self.assertNotIn(['systemctl', 'start', 'bt-nps.service'], self.commands)
        unit = self.plugin._read(self.plugin._unit_path('nps'))
        self.assertIn('ExecStart=' + self.plugin._root('nps') + '/nps service\n', unit)
        self.assertIn('ConditionPathExists=!' + self.plugin.canonical_config_dir, unit)
        self.assertEqual(self.plugin._manifest('nps')['repository'], 'ehang-io/nps')

    def test_npc_install_uses_config_file_and_no_secret_in_unit(self):
        self.mock_package('npc')
        result = self.plugin.install({'role': 'npc'})
        self.assertTrue(result['status'], result)
        unit = self.plugin._read(self.plugin._unit_path('npc'))
        self.assertIn('-config=', unit)
        self.assertIn('-debug=false', unit)
        self.assertIn('-log_level=6', unit)
        command = next(line for line in unit.splitlines() if line.startswith('ExecStart='))
        flags = {arg.split('=')[0] for arg in command.split()[1:]}
        self.assertEqual(flags, {'-config', '-debug', '-log', '-log_level', '-log_path'})
        self.assertNotIn('-vkey=', unit)
        self.assertNotIn('bt-npc.service', self.enabled)

    def test_client_autostart_enabled_after_configuration(self):
        text = self.provision('npc')
        text = text.replace('server_addr=\n', 'server_addr=nps.example:8024\n').replace('vkey=\n', 'vkey=client123456\n')
        self.plugin._write(self.plugin._config_path('npc'), text)
        result = self.plugin.service_admin({'role': 'npc', 'status': 'start'})
        self.assertTrue(result['status'], result)
        self.assertIn('bt-npc.service', self.enabled)

    def test_start_failure_not_reported_as_success(self):
        self.provision()
        self.plugin._state = lambda role: 'failed'
        result = self.plugin.service_admin({'role': 'nps', 'status': 'start'})
        self.assertFalse(result['status'])
        self.assertNotIn('bt-nps.service', self.enabled)

    def test_reinstall_preserves_all_data_and_makes_backup(self):
        text = self.provision()
        root = Path(self.plugin._root('nps'))
        self.plugin._write(str(root / 'conf' / 'clients.json'), '{"clients":[1]}')
        self.plugin._write(str(root / 'custom' / 'data.txt'), '用户数据')
        self.states['bt-nps.service'] = 'active'
        self.mock_package()
        result = self.plugin.reinstall({'role': 'nps'})
        self.assertTrue(result['status'], result)
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), text)
        self.assertEqual((root / 'custom' / 'data.txt').read_text(encoding='utf-8'), '用户数据')
        self.assertEqual((root / 'conf' / 'clients.json').read_text(encoding='utf-8'), '{"clients":[1]}')
        self.assertEqual(self.states['bt-nps.service'], 'inactive')
        backups = list(Path(self.plugin.backup_dir).glob('*.tar.gz'))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as backup:
            self.assertIn('bt-nps/conf/clients.json', backup.getnames())

    def test_missing_package_leaves_running_service_untouched(self):
        old = self.provision()
        self.states['bt-nps.service'] = 'active'
        result = self.plugin.reinstall({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertIn('缺少本地安装包', result['msg'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), old)

    def test_corrupt_package_leaves_running_service_untouched(self):
        old = self.provision()
        self.states['bt-nps.service'] = 'active'
        path = self.mock_package()
        path.write_bytes(b'X' * path.stat().st_size)
        result = self.plugin.reinstall({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertIn('SHA256', result['msg'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), old)

    def test_enable_failure_restores_previous_service(self):
        old = self.provision()
        self.states['bt-nps.service'] = 'active'
        self.enabled.add('bt-nps.service')
        self.mock_package()
        self.fail_enable = True
        result = self.plugin.reinstall({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), old)
        self.assertEqual(Path(self.plugin._root('nps'), 'nps').read_text(encoding='utf-8'), '模拟二进制')
        self.assertEqual(self.states['bt-nps.service'], 'active')

    def test_new_install_enable_failure_can_retry(self):
        self.mock_package()
        self.fail_enable = True
        result = self.plugin.install({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertFalse(Path(self.plugin._root('nps')).exists())
        self.assertFalse(Path(self.plugin._unit_path('nps')).exists())
        self.fail_enable = False
        self.assertTrue(self.plugin.install({'role': 'nps'})['status'])

    def test_retained_data_without_unit_can_be_adopted(self):
        text = self.provision()
        os.unlink(self.plugin._unit_path('nps'))
        self.assertFalse(self.plugin.get_info({'role': 'nps'})['data']['installed'])
        self.mock_package()
        result = self.plugin.install({'role': 'nps'})
        self.assertTrue(result['status'], result)
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), text)
        self.assertTrue(Path(self.plugin._unit_path('nps')).exists())

    def test_service_control_refuses_foreign_unit(self):
        self.provision()
        self.plugin._write(self.plugin._unit_path('nps'), '# 其他服务\n[Service]\n')
        result = self.plugin.service_admin({'role': 'nps', 'status': 'stop'})
        self.assertFalse(result['status'])
        self.assertEqual(self.commands, [])

    def test_reinstall_refuses_nested_symlink(self):
        self.provision()
        original = os.path.islink
        config = self.plugin._config_path('nps')
        with patch.object(module.os.path, 'islink', side_effect=lambda path: str(path) == config or original(path)):
            result = self.plugin.reinstall({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertEqual(self.commands, [])

    def test_canonical_config_directory_blocks_server_only(self):
        directory = Path(self.plugin.canonical_config_dir)
        directory.mkdir()
        (directory / 'user-data').write_text('保留', encoding='utf-8')
        result = self.plugin.install({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertIn(str(directory), result['msg'])
        self.assertEqual(self.commands, [])
        self.assertTrue((directory / 'user-data').exists())
        self.mock_package('npc')
        self.assertTrue(self.plugin.install({'role': 'npc'})['status'])

    def test_canonical_config_directory_blocks_start_but_allows_stop(self):
        self.provision()
        Path(self.plugin.canonical_config_dir).mkdir()
        result = self.plugin.service_admin({'role': 'nps', 'status': 'start'})
        self.assertFalse(result['status'])
        self.assertEqual(self.commands, [])
        info = self.plugin.get_info({'role': 'nps'})['data']
        self.assertIn(self.plugin.canonical_config_dir, info['environment_error'])
        self.assertTrue(self.plugin.service_admin({'role': 'nps', 'status': 'stop'})['status'])

    def test_unmanaged_metadata_is_not_overwritten(self):
        original = self.provision()
        manifest = Path(self.plugin._root('nps'), '.bt-plugin.json')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['repository'] = 'unknown/source'
        manifest.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.plugin._manifest('nps'), {})
        self.assertFalse(self.plugin.reinstall({'role': 'nps'})['status'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.plugin._read(self.plugin._config_path('nps')), original)

    def test_local_index_requires_ehang_source(self):
        self.put_package('nps', b'original')
        index = Path(self.plugin.package_dir, 'packages.json')
        data = json.loads(index.read_text(encoding='utf-8'))
        data['repository'] = 'unknown/source'
        index.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '来源或版本'):
            self.plugin._local_asset('nps')

    def test_refuse_unmanaged_installation(self):
        root = Path(self.plugin._root('nps'))
        root.mkdir()
        (root / 'important').write_text('保留', encoding='utf-8')
        result = self.plugin.install({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertTrue((root / 'important').exists())
        self.assertEqual(self.commands, [])

    def test_refuse_foreign_systemd_unit(self):
        self.plugin._write(self.plugin._unit_path('nps'), '# 用户服务\n[Service]\n')
        result = self.plugin.install({'role': 'nps'})
        self.assertFalse(result['status'])
        self.assertEqual(self.commands, [])

    @unittest.skipUnless((ROOT / 'nps' / 'packages.json').is_file(), '先运行 build_nps.py 生成本地包索引')
    def test_supplied_local_archives_install_without_network(self):
        self.plugin.package_dir = str(ROOT / 'nps')
        self.plugin._architecture = lambda: 'amd64'
        for role in ('nps', 'npc'):
            with self.subTest(role=role):
                result = self.plugin.install({'role': role})
                self.assertTrue(result['status'], result)
                config = self.plugin._read(self.plugin._config_path(role))
                self.plugin._validate(role, config)
                program = Path(self.plugin._root(role), role).read_bytes()
                self.assertTrue(self.plugin.version.lstrip('v').encode('ascii') in program, '请核实本地程序是否为插件适配的版本')
                self.assertTrue(Path(self.plugin._unit_path(role)).exists())

    def test_old_plugin_files_not_referenced(self):
        for name in ('nps_main.py', 'index.html', 'install.sh', 'info.json'):
            text = (ROOT / 'nps' / name).read_text(encoding='utf-8')
            self.assertNotIn('/usr/local/frp', text)
            self.assertNotIn('name=frp', text)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.nps-build-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.plugin = self.base / 'nps'
        self.plugin.mkdir()
        self.location = patch.object(builder, 'PLUGIN', self.plugin)
        self.location.start()
        self.addCleanup(self.location.stop)

    def make_archive(self, machine=62, version=b'0.26.10', origin=b'ehang.io/nps'):
        header = b'\x7fELF\x02\x01' + b'\x00' * 12 + machine.to_bytes(2, 'little')
        content = archive_bytes({'npc': header + version + b'\x00' + origin + b'\r\n\x00\xff' * 100, 'conf/npc.conf': '[common]\n'})
        path = self.plugin / 'linux_amd64_client.tar.gz'
        path.write_bytes(content)
        return path, content

    def test_collect_local_package_hashes(self):
        path, content = self.make_archive()
        assets = builder.collect_packages()
        self.assertEqual(set(assets), {path.name})
        self.assertEqual(assets[path.name]['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(assets[path.name]['size'], len(content))

    def test_build_rejects_wrong_architecture(self):
        self.make_archive(machine=183)
        with self.assertRaisesRegex(ValueError, '架构'):
            builder.collect_packages()

    def test_build_rejects_unknown_origin_or_version(self):
        for options in ({'origin': b'unknown/source'}, {'version': b'0.0.0'}):
            with self.subTest(options=options):
                self.make_archive(**options)
                with self.assertRaisesRegex(ValueError, '来源或版本'):
                    builder.collect_packages()

    def test_build_requires_ehang_server_config(self):
        header = b'\x7fELF\x02\x01' + b'\x00' * 12 + (62).to_bytes(2, 'little')
        entries = {'nps': header + b'ehang.io/nps\x000.26.10', 'conf/nps.conf': 'appname=nps\n',
                   'web/views/index.html': '页面', 'web/static/main.css': '样式'}
        path = self.plugin / 'linux_amd64_server.tar.gz'
        path.write_bytes(archive_bytes(entries))
        with self.assertRaisesRegex(ValueError, '配置模板'):
            builder.collect_packages()
        entries['conf/nps.conf'] += 'bridge_type=tcp\nbridge_port=8024\n'
        path.write_bytes(archive_bytes(entries))
        self.assertIn(path.name, builder.collect_packages())

    def test_build_rejects_empty_package_directory(self):
        with self.assertRaisesRegex(ValueError, '没有可用的安装包'):
            builder.collect_all('v0.26.10')

    def test_build_merges_optional_online_hashes(self):
        content = b'online-only'
        digest = hashlib.sha256(content).hexdigest()
        (self.plugin / 'online_hashes.json').write_text(
            json.dumps({'linux_arm64_server.tar.gz': {'size': len(content), 'sha256': digest}}), encoding='utf-8')
        assets = builder.collect_all('v0.26.10')
        self.assertEqual(assets['linux_arm64_server.tar.gz'], {'size': len(content), 'sha256': digest})

    def test_build_rejects_invalid_online_hashes(self):
        for data in ('broken', '[]', json.dumps({'../evil.tar.gz': {'size': 1, 'sha256': '0' * 64}}),
                     json.dumps({'linux_arm64_server.tar.gz': {'size': 0, 'sha256': '0' * 64}}),
                     json.dumps({'linux_arm64_server.tar.gz': {'size': 1, 'sha256': 'abc'}})):
            with self.subTest(data=data):
                (self.plugin / 'online_hashes.json').write_text(data, encoding='utf-8')
                with self.assertRaises(ValueError):
                    builder.collect_all('v0.26.10')

    def test_zip_keeps_only_bundled_archives(self):
        for name in builder.FILES:
            if name != 'icon.png':
                (self.plugin / name).write_bytes((ROOT / 'nps' / name).read_bytes())
        digest = hashlib.sha256(b'remote').hexdigest()
        (self.plugin / 'online_hashes.json').write_text(
            json.dumps({'linux_arm64_client.tar.gz': {'size': 6, 'sha256': digest}}), encoding='utf-8')
        with patch.object(builder, 'ROOT', self.base):
            builder.main()
        with zipfile.ZipFile(self.base / 'nps.zip') as package:
            names = package.namelist()
            self.assertNotIn('nps/linux_arm64_client.tar.gz', names)
            manifest = json.loads(package.read('nps/packages.json'))
            self.assertEqual(manifest['packages']['linux_arm64_client.tar.gz']['sha256'], digest)

    def test_offline_zip_preserves_binary_payload(self):
        path, content = self.make_archive()
        for name in builder.FILES:
            if name != 'icon.png':
                (self.plugin / name).write_bytes((ROOT / 'nps' / name).read_bytes())
        with patch.object(builder, 'ROOT', self.base):
            builder.main()
        with zipfile.ZipFile(self.base / 'nps.zip') as package:
            self.assertEqual(package.read('nps/' + path.name), content)
            manifest = json.loads(package.read('nps/packages.json'))
            self.assertEqual(manifest['version'], module.nps_main.version)
            self.assertEqual(manifest['repository'], 'ehang-io/nps')
            self.assertEqual(manifest['packages'][path.name]['sha256'], hashlib.sha256(content).hexdigest())
            self.assertNotIn(b'\r\n', package.read('nps/install.sh'))
        self.assertEqual(path.read_bytes(), content)


if __name__ == '__main__':
    unittest.main(verbosity=2)
