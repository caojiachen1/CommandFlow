"""坐标拾取工具 - 获取鼠标位置的物理像素坐标（用于工作流节点配置）"""
import sys
import time

# 设置 DPI Awareness
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except:
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            except:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except:
                    pass
    except:
        pass

from automation_runtime import get_system_dpi_scale

try:
    import pyautogui
except ImportError:
    print("错误: 需要安装 pyautogui")
    print("运行: pip install pyautogui")
    sys.exit(1)

def main():
    dpi_scale = get_system_dpi_scale()
    
    print("=" * 70)
    print("坐标拾取工具 - 物理像素模式")
    print("=" * 70)
    print(f"\n系统 DPI 缩放: {dpi_scale}x ({dpi_scale*100:.0f}%)")
    print("坐标模式: 物理像素（DPI 缩放已禁用）")
    print("\n使用说明:")
    print("  1. 将鼠标移动到你想点击的位置")
    print("  2. 按 Ctrl+C 停止程序")
    print("  3. 使用显示的「物理坐标」填入工作流节点\n")
    print("正在实时显示坐标...\n")
    print("-" * 70)
    print(f"{'物理坐标 (填入节点)':^40}")
    print("-" * 70)
    
    try:
        last_pos: tuple[int, int] | None = None
        while True:
            # 获取物理坐标
            physical_x, physical_y = pyautogui.position()
            
            current_pos = (physical_x, physical_y)
            if current_pos != last_pos:
                print(f"({physical_x:4}, {physical_y:4})                                ", end='\r')
                last_pos = current_pos
            
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n\n" + "-" * 70)
        if last_pos is not None:
            px, py = last_pos
            print(f"最后位置:")
            print(f"  物理坐标: ({px}, {py}) ← 直接复制这个到节点配置")
        print("\n程序已退出")

if __name__ == "__main__":
    main()
