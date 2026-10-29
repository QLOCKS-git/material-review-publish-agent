# 素材审核与发布排期 Agent

题号 1；交付仓库：https://github.com/QLOCKS-git/material-review-publish-agent。CLI 接收素材、发布时间要求及本地业务资料，模型选择工具动作，观察结果后补查、调整建议时段、保存草稿或结束。项目不提供真实发布操作。

## 环境与入口

Python 3.12，应用仅使用标准库，无需安装第三方 Python 依赖。
运行命令均在项目根目录执行。

离线流程检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m src.cli evaluate --mode mock
```

Mock 是确定性策略，只验证工具、约束及流程，不能证明真实模型决策效果。每份输出和 Trace 均标明 mode 与 evidence_scope。

真实模型使用本地 Qwen3-4B-Instruct-2507（Q4_K_M，约 2.5 GB，Apache 2.0），无需云端密钥。实际验证使用 llama.cpp 的本地 HTTP 服务，应用没有 Agent 框架依赖。以下命令适用于 Ubuntu 24.04 / WSL；首次需要联网下载源码和权重，之后可离线运行。

```bash
sudo apt-get update
sudo apt-get install -y cmake g++ make curl libssl-dev
export MODEL_HOME="$HOME/.cache/material-agent"
export LLAMA_REV=2e0d86bebc210947680e8f0d67daa79f3260c106
mkdir -p "$MODEL_HOME/runtime" "$MODEL_HOME/models"
curl -fL --retry 3 "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/$LLAMA_REV" -o "$MODEL_HOME/runtime/llama.tar.gz"
tar -xzf "$MODEL_HOME/runtime/llama.tar.gz" -C "$MODEL_HOME/runtime"
cmake -S "$MODEL_HOME/runtime/llama.cpp-$LLAMA_REV" -B "$MODEL_HOME/runtime/llama.cpp-$LLAMA_REV/build" \
  -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
cmake --build "$MODEL_HOME/runtime/llama.cpp-$LLAMA_REV/build" --target llama-server -j 8

export MODEL_SHA=85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9
curl -fL --retry 3 -C - "https://registry.ollama.ai/v2/library/qwen3/blobs/sha256:$MODEL_SHA" \
  -o "$MODEL_HOME/models/qwen3-4b-instruct-2507-q4_k_m.gguf"
echo "$MODEL_SHA  $MODEL_HOME/models/qwen3-4b-instruct-2507-q4_k_m.gguf" | sha256sum -c -
"$MODEL_HOME/runtime/llama.cpp-$LLAMA_REV/build/bin/llama-server" \
  -m "$MODEL_HOME/models/qwen3-4b-instruct-2507-q4_k_m.gguf" \
  --alias qwen3:4b-instruct-2507-q4_K_M --host 127.0.0.1 --port 11434 \
  --ctx-size 16384 --parallel 1 --threads 14 --threads-batch 14 --jinja
```

在另一个终端进入项目根目录执行：

```bash
python3 -m src.cli run --mode real --input data/scenarios/time_conflict.json
python3 -m src.cli evaluate --mode real
```

服务仅监听本机。实际 CPU 验证环境与模型校验值见 examples/real/runtime.json；低配置机器可以减少线程并调高 MODEL_TIMEOUT。模型权重和运行时不放入仓库。
如已安装 [Ollama](https://docs.ollama.com/linux)，也可执行 ollama pull qwen3:4b-instruct-2507-q4_K_M，再设置 MODEL_BACKEND=ollama 使用其 /api/chat 接口；该适配入口与实际验证的 llama.cpp 后端应分别看待。

.env.example 给出后端、服务地址、模型名和超时默认值；默认使用 llama.cpp，无需密钥。环境变量或根目录 .env 可覆盖这些值。真实模式失败不会自动改用 Mock。

输出默认写入 .state/output.json 与 .state/trace.json，草稿保存在 .state/drafts.sqlite。
可通过 --db、--output、--trace、--max-steps 修改。查看已保存草稿：

```bash
python3 -m src.cli drafts --db .state/drafts.sqlite
```

## 评审入口

- [需求和成功标准](docs/01-requirements.md)
- [决策流程和设计取舍](docs/02-solution.md)
- [测试、实际结果及限制](docs/03-evaluation.md)
- [AI 使用范围与贡献记录](docs/04-ai-usage.md)
- [复盘](docs/05-retrospective.md)
- [离线运行记录](examples/mock/evaluation.json)
- [真实模型验收记录](examples/real/evaluation.json)：9/9 通过，覆盖全部六项业务验收。
- [验证环境与模型参数](examples/real/runtime.json)、[改进前的真实失败记录](examples/observed-failure/)。
- [可执行测试结果](examples/test-results.json)

examples 中的输入、输出、Trace 和草稿快照由运行命令生成，不使用预写决策日志充当模型效果。

## 使用与业务边界

输入文件包含 request 与 world。request 指定 request_id 及 items，每项包括 material_id、channel、requested_at（含时区）、hard_time、approved_alternatives 和 approved_risks。
world 是合成素材、规则、可用时段、已有占用和可控工具故障；真实模型只通过业务工具观察 world，不直接读取预期答案。

工具为 read_material、check_rules、query_slots、save_draft。模型每轮返回一个结构化动作和一句基于可见结果的执行依据，程序执行工具后再次调用模型。
默认最多 16 个决策步骤，上限包含无效动作与 finish；超限输出 INCOMPLETE 和 STEP_LIMIT。

- requested_at 始终保留原要求。未经用户同意的硬性时间变更以 PENDING 草稿保存，不能标为已确认。
- 审核阻断或资料不完整时不能保存排期。
- 相同 request_id/material_id 只有一份草稿；重复保存返回 UNCHANGED。
- 占用变化后重新读取资料与时段，更新原草稿而非复用冲突时间。原始硬约束变化须使用新的 request_id；用户补充的确认项可用于确认原建议。
- 保存状态和草稿 ID 由实际工具结果生成，最终输出固定 published=false。
- SQLite 为草稿时段设置唯一约束，事务中保存并复核资料版本。当前为本地工具，不连接真实内容或发布系统。

## 场景与验证

data/scenarios 提供正常、资料缺失、规则阻断、时段冲突、重复提交、保存失败，以及补充资料和风险确认场景。
重复提交会连续执行四次：首次保存、原请求重放、18:00 被占用、18:00/19:00 均被占用，检查草稿 ID 不变、记录数为一，并逐次更新建议时间。

```bash
# 生成带实际测试结果的文件
python3 -m tests.run_and_record
# 以指定目录保留不同模型或提示版本的评估结果
python3 -m src.cli evaluate --mode real --output-dir examples/real
```

业务结果可能是 COMPLETED、PENDING_CONFIRMATION、NEEDS_INFO、BLOCKED、FAILED、PARTIAL、INCOMPLETE。
COMPLETED 仅表示处理及草稿保存完成，不表示发布。
模型或执行失败时，run 命令返回非零退出码；evaluate 根据验收断言是否通过决定退出码，预期保存失败场景仍可验收通过。

GitHub Actions 在 push/pull_request 执行单元测试与离线验收；本地模型测试单独运行，不要求评审持有付费服务密钥。

## 交付

项目包含 src、tests、data、examples、docs、requirements.txt、.env.example 和 CI。模型权重、运行时、数据库和个人配置不提交到仓库。
最终交付提供题号、公开 GitHub URL 和最终 commit SHA。模型及运行版本、实际投入和验证边界记录于 docs。
