# 归属与许可说明

## 上游项目

- 项目：[amchii/tg-signer](https://github.com/amchii/tg-signer)
- 许可证：BSD 3-Clause License
- 版权：Copyright (c) 2023, Amchii
- 许可证全文：[LICENSE.upstream](LICENSE.upstream)

本仓库基于上述项目做**部署层**改造，不包含对 `tg_signer/*.py` 的修改，也不重新分发上游 Python 源码。上游作者不背书、不参与本仓库。

## 本仓库自有部分

- 许可证：MIT，见 [LICENSE](LICENSE)
- 版权：Copyright (c) 2026 z563855878
- 范围：`container/`、`deploy/`、`patrol/`、`tools/`、`patch/`、`docs/` 下的脚本与文档

## 需要注意的交叉部分

`patch/patch_tg_signer.py` 中的 `OLD_LOOP` / `OLD_MARK` 等锚点字符串是从上游 BSD-3-Clause 授权的 `tg_signer/core.py` 逐字复制的，用于在运行时定位插入点并向安装包副本注入群级进度逻辑。这部分内容适用上游许可证，须保留上游版权声明与免责声明（已随 `LICENSE.upstream` 一并提供）。

本仓库不主张对上游 tg-signer 的名称、商标或项目本身拥有任何权利。文中「原版」「改进」仅为功能描述，不构成对上游项目的贬损或背书。
