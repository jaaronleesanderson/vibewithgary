#!/usr/bin/env python3
"""
Build script for Gary Agent - creates installers for Mac, Windows, and Linux.
Run this on each platform to create the platform-specific installer.
"""

import os
import platform
import shutil
import subprocess
import sys

def build():
    system = platform.system().lower()
    
    print(f"Building Gary Agent for {platform.system()}...")
    
    # Common PyInstaller options
    opts = [
        "gary_agent.py",
        "--name=GaryAgent",
        "--onefile",
        "--clean",
        "--noconfirm",
    ]
    
    # Platform-specific options
    if system == "darwin":  # macOS
        opts.extend([
            "--windowed",
            "--osx-bundle-identifier=com.vibewithgary.agent",
        ])
        output_name = "GaryAgent-mac"
    elif system == "windows":
        opts.extend([
            "--console",  # Show console for pairing code
            "--icon=icon.ico" if os.path.exists("icon.ico") else "",
        ])
        output_name = "GaryAgent-win"
    else:  # Linux
        opts.extend([
            "--console",
        ])
        output_name = "GaryAgent-linux"
    
    # Remove empty options
    opts = [o for o in opts if o]
    
    # Run PyInstaller
    cmd = [sys.executable, "-m", "PyInstaller"] + opts
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    
    # Rename output
    dist_dir = "dist"
    if system == "darwin":
        src = os.path.join(dist_dir, "GaryAgent")
        dst = os.path.join(dist_dir, output_name)
    elif system == "windows":
        src = os.path.join(dist_dir, "GaryAgent.exe")
        dst = os.path.join(dist_dir, f"{output_name}.exe")
    else:
        src = os.path.join(dist_dir, "GaryAgent")
        dst = os.path.join(dist_dir, output_name)
    
    if os.path.exists(src) and src != dst:
        if os.path.exists(dst):
            os.remove(dst) if os.path.isfile(dst) else shutil.rmtree(dst)
        os.rename(src, dst)
    
    print(f"\nBuild complete! Output: {dst}")
    print("\nTo create a proper installer:")
    if system == "darwin":
        print("  - Use 'hdiutil' to create a .dmg")
        print("  - Or use 'create-dmg' from Homebrew")
    elif system == "windows":
        print("  - Use NSIS or Inno Setup for a proper installer")
        print("  - Or distribute the .exe directly")
    else:
        print("  - Use 'appimagetool' to create an AppImage")
        print("  - Or distribute the binary directly")

if __name__ == "__main__":
    build()
