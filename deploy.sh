#!/usr/bin/env bash
# 构建并启动容器。两处环境坑都在这里固化掉了。
#
# 1) compose v1 读取由 Docker 29 创建的容器会抛 `KeyError: 'ContainerConfig'`。
#    所以必须先 down 再 up；直接 `up --build` 在重建已有容器时会崩。
#
# 2) data/ 与 storage/ 的属主可能已经被容器里的 uid 10001 接管（应用以非 root 运行）。
#    普通用户执行 docker build 时，Docker 打包上下文要 stat 这两个目录，会失败并报
#    `can't stat storage`。用 sudo 跑本脚本即可 —— **不要去改这两个目录的权限位**，
#    那会让容器里的进程反过来写不进去。

set -euo pipefail
cd "$(dirname "$0")"

echo "==> 先停掉并移除旧容器（规避 compose v1 的 ContainerConfig 报错）"
docker compose down --remove-orphans || true

echo "==> 构建并启动"
docker compose up -d --build

echo
echo "==> 当前状态"
docker compose ps
echo
echo "若构建时报 can't stat storage：那是 data/ storage/ 的属主已被容器里的 uid 10001"
echo "接管，普通用户读不到构建上下文。用 sudo 再跑一次本脚本即可；不要改权限位。"
