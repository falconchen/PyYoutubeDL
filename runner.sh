#!/bin/bash
./stop.py
nohup ./downloader.py >/dev/null 2>&1 &
nohup ./webdav_uploader.py >/dev/null 2>&1 &

