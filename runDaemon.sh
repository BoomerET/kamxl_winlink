#!/usr/bin/bash
cd /home/daveb/Projects/GIT/kamxl_winlink
source ~/.venv/bin/activate
python3 kamxl_daemon.py --port /dev/kamxl --verbose
