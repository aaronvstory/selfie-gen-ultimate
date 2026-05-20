# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Kling UI - Direct GUI Launcher
Build with: pyinstaller kling_gui_direct.spec --noconfirm

Produces: dist/KlingUI/KlingUI.exe  (one-folder mode for reliability)
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

SPEC_DIR = Path(SPECPATH)
ICON_PATH = str(SPEC_DIR / 'kling_ui.ico')
ICON_PNG_PATH = str(SPEC_DIR / 'kling_ui.png')  # macOS/Linux iconphoto source

# -----------------------------------------------------------------------
# Hidden imports
# -----------------------------------------------------------------------
hiddenimports = [
    # App-local modules
    'path_utils',
    'model_metadata',
    'model_schema_manager',
    'kling_generator_falai',
    'balance_tracker',
    'selenium_balance_checker',
    'dependency_checker',

    # Kling GUI package (explicit)
    'kling_gui',
    'kling_gui.main_window',
    'kling_gui.config_panel',
    'kling_gui.drop_zone',
    'kling_gui.log_display',
    'kling_gui.queue_manager',
    'kling_gui.video_looper',
    'kling_gui.theme',
    'kling_gui.model_manager_dialog',

    # Tkinter
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'tkinter.simpledialog',
    'tkinterdnd2',
    'tkinterdnd2.TkinterDnD',

    # PIL / Pillow
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'PIL.ImageDraw',
    'PIL.ImageFont',

    # Rich
    'rich',
    'rich.console',
    'rich.progress',
    'rich.panel',
    'rich.text',
    'rich.table',
    'rich.live',
    'rich.spinner',
    'rich.markup',

    # Requests
    'requests',
    'requests.adapters',
    'requests.auth',
    'requests.packages',
    'fal_client',
    'urllib3',
    'urllib3.util',
    'certifi',

    # Standard library
    'json',
    'logging',
    'logging.handlers',
    'threading',
    'concurrent.futures',
    'urllib.request',
    'urllib.parse',
    'base64',
    'hashlib',
    'webbrowser',
    'copy',
]

# Selenium submodules (all of them - avoids runtime import failures)
hiddenimports += collect_submodules('selenium')
hiddenimports += collect_submodules('webdriver_manager')

# Similarity engine ML stack — DeepFace + ArcFace + Facenet512 + anti-spoofing.
# These pull in tensorflow + tf-keras + retinaface + torch (FAS classifier).
# PyInstaller can't statically trace DeepFace's lazy model loading, so collect_submodules
# is required to ensure the frozen build can run face similarity at runtime.
hiddenimports += [
    'similarity_engine',
    'face_similarity',
    'deepface',
    'tf_keras',
    'retinaface',
    'torch',
]
try:
    hiddenimports += collect_submodules('deepface')
except Exception:
    pass
try:
    hiddenimports += collect_submodules('retinaface')
except Exception:
    pass
# torch submodules can be heavy; collect only what FAS needs.
hiddenimports += [
    'torch.nn',
    'torch.nn.functional',
    'torch.utils',
    'torch.utils.data',
]

# -----------------------------------------------------------------------
# Data files (non-Python resources)
# -----------------------------------------------------------------------

# tkinterdnd2 platform libraries (DLLs + TCL scripts)
datas = collect_data_files('tkinterdnd2')

# certifi CA bundle
datas += collect_data_files('certifi')

# App icon (bundled so _set_app_icon can find it in _MEIPASS).
# Both .ico and .png are shipped — Windows uses .ico via iconbitmap,
# macOS/Linux use .png via iconphoto (Tk on Aqua silently ignores .ico).
if Path(ICON_PATH).exists():
    datas.append((ICON_PATH, '.'))
if Path(ICON_PNG_PATH).exists():
    datas.append((ICON_PNG_PATH, '.'))

# Default config template (prompts, model defaults - no API key)
template_path = str(SPEC_DIR / 'default_config_template.json')
if Path(template_path).exists():
    datas.append((template_path, '.'))

# models.json (model list — editable by user, bundled as default)
models_json_path = str(SPEC_DIR / 'models.json')
if Path(models_json_path).exists():
    datas.append((models_json_path, '.'))

# Oldcam scripts and launchers
for oldcam_dir_name in ('oldcam-v7', 'oldcam-v8'):
    oldcam_dir = SPEC_DIR / oldcam_dir_name
    if oldcam_dir.exists():
        for oldcam_file in oldcam_dir.rglob('*'):
            if oldcam_file.is_file() and '__pycache__' not in oldcam_file.parts:
                target_dir = Path(oldcam_dir_name) / oldcam_file.relative_to(oldcam_dir).parent
                datas.append((str(oldcam_file), str(target_dir)))

# Standalone similarity app bundle (lean copy; skip cache/dev artifacts)
similarity_dir = SPEC_DIR / 'similarity'
if similarity_dir.exists():
    similarity_skip_dirs = {'.git', '.venv', '__pycache__', '.pytest_cache', '.serena'}
    similarity_skip_files = {'config.json', 'manifest.json'}
    similarity_skip_paths = {
        Path('src') / 'models',
    }
    for similarity_file in similarity_dir.rglob('*'):
        if not similarity_file.is_file():
            continue
        relative_path = similarity_file.relative_to(similarity_dir)
        if similarity_skip_dirs.intersection(relative_path.parts):
            continue
        if any(skip_path in relative_path.parents for skip_path in similarity_skip_paths):
            continue
        if similarity_file.name in similarity_skip_files:
            continue
        if similarity_file.name == '.DS_Store':
            continue
        if similarity_file.suffix.lower() == '.zip':
            continue
        target_dir = Path('similarity') / relative_path.parent
        datas.append((str(similarity_file), str(target_dir)))

# -----------------------------------------------------------------------
# Analysis
# -----------------------------------------------------------------------
a = Analysis(
    [str(SPEC_DIR / 'gui_launcher.py')],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(SPEC_DIR / 'hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Cut down bloat - these are never used at runtime
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'IPython', 'notebook', 'jupyter',
        'PyQt5', 'PyQt6', 'wx',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KlingUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX disabled: reduces AV false positives
    console=False,   # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_PATH if Path(ICON_PATH).exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='KlingUI',
)
