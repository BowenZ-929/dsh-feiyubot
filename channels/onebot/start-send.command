#!/bin/bash
# 双击这个 = 双击 start.command 的 send 版（她会真的把话发出去）。
# 本文件只负责把模式定成 send，其余逻辑全在 start.command，别在这里复制一份。
exec "$(dirname "$0")/start.command" send
