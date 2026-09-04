---
type: derived
source: projects/omlxc
owner: governance-team
last_updated: 2026-09-03
---

# y7000p 语音转写服务 (asr)

> 2026-08-25 部署。数字大脑语音入口(会议/语音邮件)的算力层。

## 文件

| 文件 | 用途 |
|---|---|
| `whisper_http.py` | HTTP 服务: `POST /asr`(audio_b64+language→text) / `GET /health`。CPU int8 tiny, 零依赖(标准库), 内置 CUDA12 DLL 注入(GPU 启用时用) |
| `asr_test.py` | 一次性验证脚本(SAPI 合成→转写比对), `ASR_DEVICE=cpu\|cuda` 可切 |
| `start_whisper.bat` | 启动器(重定向日志到 wh_http.log) |
| `test_post.py` | 本机 POST 全链自测 |

## 部署位置与状态 (2026-08-25 更新: 全链贯通)

- y7000p `C:\Users\xia\`, schtasks 任务 `whisper_http`(onlogon) 已建
- **✅ 端到端全链实测通过**: MBP → tailscale relay → y7000p 转写 → 返回。
  转写完美("Hello world, this is a whisper test on the Y7000P."),
  **服务端推理仅 0.5s**; 端到端 41.5s 中传输占 41s(relay 上行慢,
  网络瓶颈非算力瓶颈)
- **✅ 防火墙破案**: 历史遗留 2 条 `python.exe` **Block 规则**(当年首次
  监听弹窗被"取消"所留)压死一切 Allow(Block 优先级铁律) — 已删 8 条
  同名规则, `py-asr-allow`(程序级) + 8390 端口规则接管, 跨机 health 200

## 已知债

- GPU 模式: cudnn 9.24 `cudnnGetLibConfig` 符号加载失败
  (ctranslate2 4.6.0 组合问题)。CPU int8 推理 0.5s 已远超需求,
  GPU 激活降级为"顺手债", 版本对齐窗口再做
- 传输优化(可选): relay 上行 307KB/41s 偏慢 — 候选: 客户端压缩
  (opus/webm 上传)、或切 tailscale 直连(同 NAT 时)
- 常驻: schtasks onlogon 触发, 重启后自动拉起(下次登录验证)
