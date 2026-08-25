# y7000p 语音转写服务 (asr)

> 2026-08-25 部署。数字大脑语音入口(会议/语音邮件)的算力层。

## 文件

| 文件 | 用途 |
|---|---|
| `whisper_http.py` | HTTP 服务: `POST /asr`(audio_b64+language→text) / `GET /health`。CPU int8 tiny, 零依赖(标准库), 内置 CUDA12 DLL 注入(GPU 启用时用) |
| `asr_test.py` | 一次性验证脚本(SAPI 合成→转写比对), `ASR_DEVICE=cpu\|cuda` 可切 |
| `start_whisper.bat` | 启动器(重定向日志到 wh_http.log) |
| `test_post.py` | 本机 POST 全链自测 |

## 部署位置与状态 (2026-08-25)

- y7000p `C:\Users\xia\`, schtasks 任务 `whisper_http`(onlogon) 已建
- **已验证**: 本机 health 200 + 模型就绪; asr_test CPU 2.4s 完美转写
  ("Hello world, this is a whisper test on the Y7000P.")
- **待用户本机操作**:
  1. 防火墙对 `python.exe` 程序级放行(端口 allow 规则已加但疑似被既有
     程序级 Block 压住) — 管理员 PowerShell:
     `netsh advfirewall firewall add rule name="py-asr" dir=in action=allow program="C:\...\Python313\python.exe"`
  2. 跨机调用链路: tailscale relay(hkg) 高延迟(实测 ping 1.4s), 大 body
     上传慢 — y7000p 网络不稳是已知常态, 真实使用期观察

## 已知债

- GPU 模式: cudnn 9.24 `cudnnGetLibConfig` 符号加载失败
  (ctranslate2 4.6.0 组合问题), CPU int8 已够用, 待版本对齐后启用
- 常驻: schtasks onlogon 触发, 重启后自动拉起(下次登录验证)
