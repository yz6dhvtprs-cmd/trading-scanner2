#!/bin/bash
# Push master to GitHub using the saved token (~/.trade_github_token, 600).
# Standing rule: every commit gets pushed immediately.
cd /Users/vismaypatel/trading-indicators || exit 1
source .venv/bin/activate
GITHUB_TOKEN=$(cat ~/.trade_github_token) python -c "
import os
from dulwich import porcelain
porcelain.push('.', 'https://github.com/yz6dhvtprs-cmd/trading-scanner2.git',
                username='yz6dhvtprs-cmd', password=os.environ['GITHUB_TOKEN'])
print('PUSH-OK')"
