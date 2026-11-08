#!/bin/sh
# Build the process-tap helper used by recorder.py (macOS 14.4+, Command Line Tools).
swiftc -O -swift-version 5 -o "$(dirname "$0")/tap-capture" "$(dirname "$0")/main.swift"
