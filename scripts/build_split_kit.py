#!/usr/bin/env python3
"""Developer/CI packaging only; end users launch the resulting executable."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--bin-dir',type=Path,default=ROOT/'build/desktop/bin');a=p.parse_args()
    stage=ROOT/'build/kit-input';stage.mkdir(parents=True,exist_ok=True)
    links={'Phone apps (split-android-apk and split-ios)': 'https://github.com/joeydd032995-pixel/ShareCompute/actions/workflows/three-device-split.yml'}
    if os.environ.get('GITHUB_RUN_ID'):
        repo=os.environ['GITHUB_REPOSITORY'];run=os.environ['GITHUB_RUN_ID'];base=f'https://github.com/{repo}/actions/runs/{run}'
        req=urllib.request.Request(f'https://api.github.com/repos/{repo}/actions/runs/{run}/artifacts',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
        with urllib.request.urlopen(req,timeout=30) as r: artifacts=json.load(r)['artifacts']
        wanted={'split-android-apk':'Android APK','split-ios':'iPhone IPA (sign with SideStore)'}
        links={wanted[x['name']]:base+'/artifacts/'+str(x['id']) for x in artifacts if x['name'] in wanted}
        if len(links)!=2: raise RuntimeError('Matching phone artifacts are missing')
    (stage/'phone-downloads.json').write_text(json.dumps(links))
    licenses=stage/'licenses';licenses.mkdir(exist_ok=True)
    for name in ('cryptography','cffi','qrcode','pyinstaller'):
        distribution=importlib.metadata.distribution(name)
        for file in distribution.files or []:
            if any(x in file.name.lower() for x in ('license','copying','notice')):
                source=distribution.locate_file(file)
                if source.is_file(): shutil.copy2(source,licenses/(name+'-'+file.name))
    import sysconfig
    python_license=Path(sysconfig.get_path('stdlib'))/'LICENSE.txt'
    if python_license.exists(): shutil.copy2(python_license,licenses/'Python-LICENSE.txt')
    for source in (ROOT/'LICENSE',ROOT/'build/desktop/THIRD_PARTY_NOTICES.txt'):
        if source.exists():shutil.copy2(source,licenses/source.name)
    suffix='.exe' if os.name=='nt' else ''
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','ShareCompute','--paths',str(ROOT/'scripts'),'--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build/pyinstaller'),'--specpath',str(ROOT/'build'),
        '--add-data',str(ROOT/'native/split/llama-revision.txt')+':native/split',
        '--add-data',str(ROOT/'native/split/reference-capacity.json')+':native/split',
        '--add-data',str(stage/'phone-downloads.json')+':.', '--add-data',str(licenses)+':licenses']
    for name in ('sc-rpc-worker','sc-split-probe'):command+=['--add-binary',str(a.bin_dir.resolve()/(name+suffix))+':bin']
    command+=[str(ROOT/'scripts/split_launcher.py')];subprocess.run(command,check=True)
    output=ROOT/'dist/ShareCompute'
    shutil.copy2(ROOT/'docs/TEST-KIT-QUICKSTART.md',output/'START-HERE.md')
    if os.name=='nt': (output/'Start.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\nShareCompute.exe\r\n')
    else:
        start=output/'Start.sh';start.write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")"\nexec ./ShareCompute\n');start.chmod(0o755)
    shutil.make_archive(str(ROOT/'dist'/('ShareCompute-Windows' if os.name=='nt' else 'ShareCompute-Linux')),'zip',ROOT/'dist','ShareCompute')
if __name__=='__main__':main()
