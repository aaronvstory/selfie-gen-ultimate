# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the BUNDLED Windows distributable.

Build with:  pyinstaller distribution/kling_gui_bundled.spec --noconfirm
Produces:    dist/SelfieGenUltimate/SelfieGenUltimate.exe  (one-folder)

Difference from kling_gui_direct.spec (the "developer" frozen build):

  * The heavy ML stack (torch / tensorflow / tf_keras / mediapipe / deepface /
    retinaface / opencv) is DELIBERATELY EXCLUDED from the bundle. Those
    packages are ~4-8GB, ship CPU-only torch (no portable CUDA), and their
    native DLLs frequently fail to load under PyInstaller. Instead they are
    pip-installed into a side venv on first use of Face Crop / similarity, via
    the shared resolver scripts/win_resolve_python.bat. The frozen GUI invokes
    those features as a SUBPROCESS using the side-venv's python.exe, so the
    bundled CPython never has to import a pip-installed torch.

  * The personal seed config (all prompts/slots/defaults, API keys blanked) is
    bundled and seeded to %LocalAppData%\\selfie-gen-ultimate on first run only
    (see gui_launcher.py frozen bootstrap).

Result: a ~150-300MB exe that opens instantly. Cloud generation (fal.ai/BFL)
needs NO local ML and works immediately; the ML install is one-time + lazy.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# This spec lives in distribution/; the repo root is its parent.
SPEC_DIR = Path(SPECPATH)
ROOT_DIR = SPEC_DIR.parent
ICON_PATH = str(ROOT_DIR / 'kling_ui.ico')
ICON_PNG_PATH = str(ROOT_DIR / 'kling_ui.png')

# -----------------------------------------------------------------------
# Hidden imports — GUI + light deps ONLY (no ML stack)
# -----------------------------------------------------------------------
hiddenimports = [
    # App-local modules
    'path_utils',
    'app_version',
    'model_metadata',
    'model_schema_manager',
    'kling_generator_falai',
    'selfie_generator',
    'outpaint_generator',
    'vision_analyzer',
    'selfie_prompt_composer',
    'fal_utils',
    'balance_tracker',
    'selenium_balance_checker',
    'dependency_checker',
    'ml_subprocess_bridge',   # NEW: frozen -> side-venv subprocess bridge

    # Kling GUI package
    'kling_gui',
    'kling_gui.main_window',
    'kling_gui.config_panel',
    'kling_gui.drop_zone',
    'kling_gui.log_display',
    'kling_gui.queue_manager',
    'kling_gui.video_looper',
    'kling_gui.theme',
    'kling_gui.model_manager_dialog',
    'kling_gui.session_manager',
    'kling_gui.session_controller',
    'kling_gui.image_state',
    'kling_gui.carousel_widget',
    'kling_gui.compare_panel',
    'kling_gui.layout_utils',
    'kling_gui.ml_backend_env',
    'kling_gui.tag_utils',
    'kling_gui.video_discovery',
    'kling_gui.video_inspector',
    'kling_gui.video_metadata',
    'kling_gui.tabs',
    'kling_gui.tabs.face_crop_tab',
    'kling_gui.tabs.prep_tab',
    'kling_gui.tabs.selfie_tab',
    'kling_gui.tabs.outpaint_tab',
    'kling_gui.tabs.expand_tab',
    'kling_gui.tabs.video_tab',

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
    'PIL.ImageOps',

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

    # Requests / network
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
    'subprocess',
    'concurrent.futures',
    'urllib.request',
    'urllib.parse',
    'base64',
    'hashlib',
    'webbrowser',
    'copy',
    'shutil',
]

try:
    hiddenimports += collect_submodules('kling_gui')
except Exception:
    pass
hiddenimports += collect_submodules('selenium')
hiddenimports += collect_submodules('webdriver_manager')

# NOTE: deliberately NO torch / deepface / tf_keras / retinaface /
# tensorflow / mediapipe / cv2 collection here. They live in the first-run
# side venv (see the excludes list below + ml_subprocess_bridge).

# -----------------------------------------------------------------------
# Data files
# -----------------------------------------------------------------------
datas = collect_data_files('tkinterdnd2')
datas += collect_data_files('certifi')

if Path(ICON_PATH).exists():
    datas.append((ICON_PATH, '.'))
if Path(ICON_PNG_PATH).exists():
    datas.append((ICON_PNG_PATH, '.'))

# Default config template + models list
for fname in ('default_config_template.json', 'models.json'):
    p = ROOT_DIR / fname
    if p.exists():
        datas.append((str(p), '.'))

# Personal seed config (built by build_bundled_exe.py before this spec runs;
# all prompts/slots/defaults, API keys + machine paths blanked). Seeded to
# %LocalAppData% on first run only. Optional so the spec still builds without
# it (a dev build without a personal seed just falls back to the template).
seed_cfg = ROOT_DIR / 'distribution' / 'personal_seed_config.json'
if seed_cfg.exists():
    datas.append((str(seed_cfg), '.'))

# Shared Python resolver + the requirement manifests + health script — needed
# at runtime so the first-run ML side-venv install can run.
resolver = ROOT_DIR / 'scripts' / 'win_resolve_python.bat'
if resolver.exists():
    datas.append((str(resolver), 'scripts'))
for fname in ('requirements.txt', 'dependency_health_check.py'):
    p = ROOT_DIR / fname
    if p.exists():
        datas.append((str(p), '.'))

# Oldcam scripts (v7/v8 ship as data, like the developer spec)
for oldcam_dir_name in ('oldcam-v7', 'oldcam-v8'):
    oldcam_dir = ROOT_DIR / oldcam_dir_name
    if oldcam_dir.exists():
        for f in oldcam_dir.rglob('*'):
            if f.is_file() and '__pycache__' not in f.parts:
                target = Path(oldcam_dir_name) / f.relative_to(oldcam_dir).parent
                datas.append((str(f), str(target)))

# Standalone similarity app bundle (its OWN launchers create the side venv +
# install its ML deps; we ship the source so the subprocess bridge can run it).
similarity_dir = ROOT_DIR / 'similarity'
if similarity_dir.exists():
    skip_dirs = {'.git', '.venv', '.venv311', '__pycache__', '.pytest_cache', '.serena'}
    skip_files = {'config.json', 'manifest.json'}
    skip_paths = {Path('src') / 'models'}
    for f in similarity_dir.rglob('*'):
        if not f.is_file():
            continue
        rel = f.relative_to(similarity_dir)
        if skip_dirs.intersection(rel.parts):
            continue
        if any(sp in rel.parents for sp in skip_paths):
            continue
        if f.name in skip_files or f.name == '.DS_Store':
            continue
        if f.suffix.lower() == '.zip':
            continue
        datas.append((str(f), str(Path('similarity') / rel.parent)))

# -----------------------------------------------------------------------
# Analysis
# -----------------------------------------------------------------------
a = Analysis(
    [str(ROOT_DIR / 'gui_launcher.py')],
    pathex=[str(ROOT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT_DIR / 'hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The heavy ML stack lives in the first-run side venv, NOT the bundle.
        # Excluding here keeps the bundle ~150-300MB instead of ~4-8GB and
        # dodges the TF/torch-under-PyInstaller DLL-load breakage class.
        'torch', 'torchvision', 'torchaudio',
        'tensorflow', 'tensorflow_intel', 'tf_keras', 'keras',
        'mediapipe',
        'deepface', 'retina_face', 'retinaface',
        'cv2', 'opencv-python', 'opencv_python',
        # General bloat never used at runtime
        'matplotlib', 'pandas', 'scipy',
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
    name='SelfieGenUltimate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # UPX off: reduces AV false positives
    console=False,    # GUI app, no console window
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
    name='SelfieGenUltimate',
)
