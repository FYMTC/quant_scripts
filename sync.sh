#!/bin/bash
# quant_scripts auto-sync — 检测未提交变更，自动提交并推送
# 由 cron 每10分钟调用，作为 post-commit hook 的兜底

cd /config/quant_scripts || exit 1

if git diff --quiet && git diff --cached --quiet; then
    exit 0
fi

git add -A

CHANGED=$(git diff --cached --name-only | head -5 | tr '\n' ' ')
if [ -z "$CHANGED" ]; then
    exit 0
fi

MSG="auto-sync: ${CHANGED}"
if [ ${#MSG} -gt 80 ]; then
    MSG="${MSG:0:77}..."
fi

git commit -m "$MSG"
git push origin main 2>&1 | tail -1
