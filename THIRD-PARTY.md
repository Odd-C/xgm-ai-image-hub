# 第三方依赖与许可证登记

本产品是**闭源商业化**项目，所以引入第三方代码 / 模型权重 / 服务时，
**许可证是第一道闸门**，不是选完再补的手续。

## 判定标准

| 许可 | 能否用 |
| --- | --- |
| MIT / Apache-2.0 / BSD | ✅ 可以 |
| AGPL / GPL 系列 | ❌ 不可以。AGPL 的网络条款对 SaaS 是致命的：用户通过网络使用服务即触发源码披露义务 |
| CC BY-NC 系列 | ⛔ 禁商用，除非单独付费签约 |

⚠️ **只认 `LICENSE` 原文与官方 model card，不认博客 / 教程的转述。** 本仓库已被这条
坑过一次（见下表的 RMBG-2.0）。

## 已在用

| 依赖 | 许可 | 用途 | 核实来源 |
| --- | --- | --- | --- |
| [LiteLLM](https://github.com/BerriAI/litellm) | MIT（`enterprise/` 子目录另有商业许可，**不使用该目录**） | `skills/prompt_optimize` 里统一调用各家文本模型 | 仓库 LICENSE |

## 已评估但**未采用**

| 项目 | 许可 | 结论 |
| --- | --- | --- |
| [linshenkx/prompt-optimizer](https://github.com/linshenkx/prompt-optimizer)（33k★） | **AGPL-3.0** | ❌ 不可嵌入。可参考其产品形态，**不得拷贝代码或提示词模板** |
| **BRIA RMBG-2.0** | **CC BY-NC 4.0** | ⛔ 禁商用。⚠️ 中文教程里流传「Apache 2.0 可免费商用」是**错的**；官方 model card 写明商用需单独签约或走 Bria API |
| [BiRefNet](https://github.com/ZhengPeng7/BiRefNet) | MIT | ⏸ 许可干净，但**自托管推理需要服务器 CPU/GPU**，客户量一多即成为线性瓶颈 → 放弃自托管形态 |
| [rembg](https://github.com/danielgatis/rembg) | MIT | ⏸ 同上 |
| [IC-Light](https://github.com/lllyasviel/IC-Light) | Apache-2.0（**采用前需核 LICENSE 原文**） | ⏸ 同上 |
| [Pines](https://github.com/thedevdojo/pines) | MIT | ⚠️ 基于 Alpine.js + Tailwind；本项目是零框架 + 手写 CSS，为单个横幅引入两个运行时依赖不划算 |
| Immich / LibrePhotos / PhotoPrism | MIT / MIT / AGPL | ⚠️ 都是「个人相册」形态，与团队协作工作台不符；只借鉴手法（缩略图、pHash 去重） |

## 资源约束（比许可证更容易被忽略）

⭐ **不在服务器上做模型推理**。自托管推理的成本随客户数线性增长，客户一多就变成瓶颈；
走**云端 API** 的成本随用量走，不占自己的 CPU/GPU。因此抠图 / 光影这类能力即使有
MIT 许可的自托管方案（BiRefNet / rembg / IC-Light）也不采用，改为按图计费的云端服务。

## 引入新依赖时的动作

1. 找到 `LICENSE` 原文 / 官方 model card，核对许可证
2. 确认不在上表的禁用范围
3. 加到本文件的「已在用」表里，写清用途与核实来源
