# -*- coding: utf-8 -*-
"""
小念意识层 C++ 加速库构建驱动（跨平台：Windows / Linux / macOS）。

为什么用 Python 而不是 .bat/.sh：
  - Windows 的 .bat 对中文/复杂 for 循环解析极易出错；Python 本身就是 UTF-8，
    一套脚本即可在 Win(自动找 cl.exe) / Linux / macOS(g++) 上构建，换机器零配置。
  - 若编译器缺失或编译失败，打印清晰提示：小念会自动回退纯 Python，功能不变。

用法：
  python build_pyclayer.py
产物：
  src/clayer/pyclayer.pyd  (Win) 或  src/clayer/pyclayer<EXT>.so  (Linux/mac)
"""
import os
import sys
import glob
import subprocess
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
CLAYER = os.path.dirname(HERE)
sys.path.insert(0, CLAYER)  # 让 gen_cpp_config.py 能 import cl_config


def log(msg):
    print(f"[build] {msg}", flush=True)


def run(cmd, cwd, env=None):
    log(" ".join(cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=False)


def find_cl_windows():
    """直接 glob 常见 VS / Build Tools 安装路径找 Hostx64\\x64\\cl.exe（最稳，不依赖 vswhere）。"""
    roots = []
    for ev in ("ProgramFiles(x86)", "ProgramFiles"):
        p = os.environ.get(ev)
        if p:
            roots.append(p)
    roots.append(r"C:\Program Files (x86)")
    roots.append(r"C:\Program Files")
    patterns = []
    for r in roots:
        for year in ("2019", "2022", "2017"):
            for ed in ("Community", "Professional", "Enterprise", "BuildTools"):
                patterns.append(os.path.join(
                    r, "Microsoft Visual Studio", year, ed,
                    "VC", "Tools", "MSVC", "*", "bin", "Hostx64", "x64", "cl.exe"))
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if not candidates:
        return None
    # 选版本号最新的（按路径字符串排序即可，格式一致）
    candidates.sort()
    return candidates[-1]


def find_winsdk():
    """返回 (ucrt_include, um_include, shared_include, winrt_include,
              ucrt_lib, um_lib) 或 None。"""
    bases = []
    for ev in ("ProgramFiles(x86)", "ProgramFiles"):
        p = os.environ.get(ev)
        if p:
            bases.append(os.path.join(p, "Windows Kits", "10"))
    for base in bases:
        inc = os.path.join(base, "Include")
        lib = os.path.join(base, "Lib")
        if not os.path.isdir(inc) or not os.path.isdir(lib):
            continue
        # 取版本号最高的一档
        vers = sorted(os.listdir(inc))
        if not vers:
            continue
        ver = vers[-1]
        ucrt_i = os.path.join(inc, ver, "ucrt")
        um_i = os.path.join(inc, ver, "um")
        sh_i = os.path.join(inc, ver, "shared")
        wr_i = os.path.join(inc, ver, "winrt")
        ucrt_l = os.path.join(lib, ver, "ucrt", "x64")
        um_l = os.path.join(lib, ver, "um", "x64")
        if os.path.isdir(ucrt_i) and os.path.isdir(ucrt_l):
            return (ucrt_i, um_i, sh_i, wr_i, ucrt_l, um_l)
    return None


def find_python_include():
    """返回含 Python.h 的目录（优先 sysconfig，否则在 prefix/base 下非递归搜索）。"""
    cands = [sysconfig.get_path("include"), sysconfig.get_path("platinclude"),
             os.path.join(sys.prefix, "Include"), os.path.join(sys.prefix, "include"),
             os.path.join(sys.base_prefix, "Include"), os.path.join(sys.base_prefix, "include")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "Python.h")):
            return c
    for base in (sys.prefix, sys.base_prefix):
        for sub in ("Include", "include"):
            d = os.path.join(base, sub)
            if os.path.isdir(d) and glob.glob(os.path.join(d, "Python.h")):
                return d
    return None


def find_python_lib():
    """返回含 python3.lib 的目录（非递归搜索常见子目录）。"""
    for base in (sys.prefix, sys.base_prefix):
        for sub in ("libs", "Lib", "lib"):
            d = os.path.join(base, sub)
            if os.path.isdir(d) and glob.glob(os.path.join(d, "python3*.lib")):
                return d
    for base in (sys.prefix, sys.base_prefix):
        try:
            for name in os.listdir(base):
                d = os.path.join(base, name)
                if os.path.isdir(d) and glob.glob(os.path.join(d, "python3*.lib")):
                    return d
        except OSError:
            pass
    return None


def main():
    is_win = sys.platform.startswith("win")
    py = sys.executable
    pyinc = find_python_include()
    if not pyinc:
        log("ERROR: 未找到 Python.h（Python 开发头文件）。请安装 Python 开发包。")
        return 1
    pylib = find_python_lib()
    if not pylib:
        log("ERROR: 未找到 python3.lib（Python 导入库）。")
        return 1
    try:
        import pybind11
        pbinc = pybind11.get_include()
    except Exception:
        log("ERROR: 未安装 pybind11（pip install pybind11）")
        return 1

    # 1) 生成 cpp_config.h（保证 C++/Python 常量一致）
    log("生成 cpp_config.h (来自 cl_config.py) ...")
    try:
        run([py, "gen_cpp_config.py"], cwd=HERE)
    except Exception as e:
        log(f"ERROR: cpp_config.h 生成失败: {e}")
        return 1
    if not os.path.exists(os.path.join(HERE, "cpp_config.h")):
        log("ERROR: cpp_config.h 未生成")
        return 1

    # 2) 编译
    if is_win:
        cl = find_cl_windows()
        if not cl:
            log("ERROR: 未找到 MSVC(cl.exe)。请安装 VS Build Tools 勾选『使用 C++ 的桌面开发』。")
            log("小念将自动使用纯 Python 实现，功能不受影响。")
            return 1
        log(f"cl.exe: {cl}")
        # 从 cl 路径推导 VCROOT = ...\VC\Tools\MSVC\<ver>
        cl_norm = cl.replace("/", "\\")
        marker = "VC\\Tools\\MSVC"
        mi = cl_norm.lower().find(marker.lower())
        seg_parts = cl_norm[mi:].split("\\")  # ['VC','Tools','MSVC','<ver>','bin',...]
        vcroot = cl_norm[:mi] + marker + "\\" + seg_parts[3]
        sdk = find_winsdk()
        if not sdk:
            log("ERROR: 未找到 Windows SDK。")
            return 1
        ucrt_i, um_i, sh_i, wr_i, ucrt_l, um_l = sdk
        env = dict(os.environ)
        env["INCLUDE"] = ";".join([pyinc, pbinc, os.path.join(vcroot, "include"),
                                    ucrt_i, um_i, sh_i, wr_i])
        env["LIB"] = ";".join([pylib, os.path.join(vcroot, "lib", "x64"), ucrt_l, um_l])
        out = os.path.join(CLAYER, "pyclayer.pyd")
        cmd = [cl, "/O2", "/EHsc", "/std:c++17", "/LD", "/utf-8",
               "pyclayer.cpp", f"/I{pyinc}", f"/I{pbinc}",
               "/link", f"/OUT:{out}", f"/LIBPATH:{pylib}", "python3.lib"]
        rc = run(cmd, cwd=HERE, env=env).returncode
        if rc != 0 or not os.path.exists(out):
            log("ERROR: 编译失败。小念将自动回退纯 Python 实现。")
            return 1
        log(f"OK -> {out}")
    else:
        ext = sysconfig.get_config_var("EXT_SUFFIX")
        out = os.path.join(CLAYER, f"pyclayer{ext}")
        import shutil
        if shutil.which("g++") is None:
            log("ERROR: 未找到 g++。请安装 build-essential / clang。")
            log("小念将自动使用纯 Python 实现，功能不受影响。")
            return 1
        cmd = ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", "-Wall",
               f"-I{pyinc}", f"-I{pbinc}", "pyclayer.cpp",
               "-o", out, f"-L{pylib}", "-lpython3"]
        rc = run(cmd, cwd=HERE).returncode
        if rc != 0 or not os.path.exists(out):
            log("ERROR: 编译失败。小念将自动回退纯 Python 实现。")
            return 1
        log(f"OK -> {out}")

    log("小念启动将自动加载 C++ 加速库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
