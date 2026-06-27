#!/bin/bash
# 量化环境激活脚本
# 使用: source quant_env.sh

source ~/quant_env/bin/activate
export QLIB_DATA_DIR=/root/ai_trading_package/qlib_data
export QUANT_SCRIPTS=/root/ai_trading_package/quant/quant_scripts
echo "量化环境已激活"
echo "  数据目录: $QLIB_DATA_DIR"
echo "  脚本目录: $QUANT_SCRIPTS"
echo ""
echo "可用命令:"
echo "  python3 \$QUANT_SCRIPTS/data_converter.py --codes 002594 518880  # 更新数据"
echo "  python3 \$QUANT_SCRIPTS/ai_factor_miner.py --code 002594          # 因子挖掘"
