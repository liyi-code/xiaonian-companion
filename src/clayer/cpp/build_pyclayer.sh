#!/usr/bin/env bash
# 小念意识层 C++ 加速库构建（Linux/macOS 薄包装）。真正逻辑在 build_pyclayer.py。
cd "$(dirname "$0")"
"${PYTHON:-python3}" build_pyclayer.py
