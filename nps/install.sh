#!/bin/bash
# 插件安装仅注册管理界面，程序由页面中的安装按钮部署。
set -euo pipefail
PATH=/www/server/panel/pyenv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
PLUGIN_DIR=/www/server/panel/plugin/nps

check_environment() {
    if [ "$(id -u)" -ne 0 ]; then
        echo '请使用 root 权限操作宝塔插件'
        exit 1
    fi
    if [ "$(uname -s)" != 'Linux' ] || [ ! -d /www/server/panel ]; then
        echo '本插件仅支持 Linux 宝塔面板'
        exit 1
    fi
}

install_plugin() {
    if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
        echo '需要使用 systemd 的 Linux 系统'
        exit 1
    fi
    mkdir -p "$PLUGIN_DIR"
    chmod 755 "$PLUGIN_DIR"
    echo 'NPS 管理器安装完成，请进入插件页面使用本地包或在线包安装 nps / npc'
    echo '适配 ehang-io/nps v0.26.10；本地包和 packages.json 校验索引随插件提供，在线包从 GitHub 官方 Release 下载'
    echo '两种方式都会校验固定的 SHA256；本地安装无需联网'
    echo '运行目录独立于原 FRP 插件，不会修改已有 FRP 配置'
    echo '如果存在 /etc/nps，服务端会拒绝启动，请先人工确认并处理该目录'
}

uninstall_plugin() {
    local role unit unit_file
    # 先核实两个服务的归属，避免误操作手工创建的同名服务。
    for role in nps npc; do
        unit_file="/etc/systemd/system/bt-${role}.service"
        if [ -f "$unit_file" ] && ! grep -qx '# bt-nps-plugin' "$unit_file"; then
            echo "拒绝卸载：${unit_file} 不属于本插件"
            exit 1
        fi
    done
    for role in nps npc; do
        unit="bt-${role}.service"
        unit_file="/etc/systemd/system/${unit}"
        if [ -f "$unit_file" ]; then
            systemctl stop "$unit"
            systemctl disable "$unit"
            rm -f -- "$unit_file"
        fi
    done
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload
    fi
    rm -rf -- "$PLUGIN_DIR"
    echo 'NPS 管理器及自启动服务已卸载'
    echo '配置、隧道数据和日志保留在 /usr/local/bt-nps 与 /usr/local/bt-npc'
    echo '重装备份保留在 /www/backup/nps；再次安装插件后可重新接管这些目录'
}

case "${1:-}" in
    install)
        check_environment
        install_plugin
        ;;
    uninstall)
        check_environment
        uninstall_plugin
        ;;
    *)
        echo '用法：bash install.sh install|uninstall'
        exit 2
        ;;
esac
