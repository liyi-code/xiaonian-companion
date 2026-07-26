import sys
import os
import traceback

print(">>> 启动 ai-girlfriend ...", flush=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


try:
    import tkinter as tk
    print(">>> tkinter 导入成功", flush=True)
    from gui import App
    print(">>> gui 导入成功", flush=True)
except Exception:
    _log("IMPORT ERROR:\n" + traceback.format_exc())
    print("导入失败，详见 error.log", flush=True)
    raise


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("RUNTIME ERROR:\n" + traceback.format_exc())
        print("运行出错，详见 error.log", flush=True)
