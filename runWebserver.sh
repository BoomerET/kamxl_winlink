#!/usr/bin/bash
cd /home/daveb/Projects/GIT/kamxl_winlink
source ~/.venv/bin/activate
python3 kamxl_rest.py --daemon-socket /tmp/kamxl.sock --port 8080
