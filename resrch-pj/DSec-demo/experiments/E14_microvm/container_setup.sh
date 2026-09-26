#!/bin/bash
# E14 容器内环境准备 + 执行 microVM 实验
set -e
export DEBIAN_FRONTEND=noninteractive
sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources
apt-get update -qq >/dev/null
apt-get install -y -qq qemu-system-x86 wget python3 >/dev/null 2>&1
echo "=== /dev/kvm: $(ls -la /dev/kvm 2>&1)"
echo "=== qemu: $(qemu-system-x86_64 --version | head -1)"
ls -la /dev/kvm
python3 /src/run_inner.py 2>&1 | tail -35
