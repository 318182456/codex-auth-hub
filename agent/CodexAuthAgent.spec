# -*- mode: python ; coding: utf-8 -*-
a = Analysis(['auth_agent.py'], pathex=[], binaries=[], datas=[], hiddenimports=['win32timezone'],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='CodexAuthAgent',
          debug=False, strip=False, upx=True, console=True)
