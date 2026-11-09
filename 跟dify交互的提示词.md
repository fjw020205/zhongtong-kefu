一、整个项目最终架构
先不要急着写代码，先理解整个系统长什么样。
把这张图记在脑子里，后面每一步都是在拼这个拼图：
                    Windows 本机
            ┌──────────────────────────┐
            │                          │
            │ Python + Playwright      │
            │                          │
            │ ① 自动打开中通客服页面   │
            │ ② 自动发送消息           │
            │ ③ 自动读取客服回复       │
            │ ④ 把收到的消息发给 Dify  │
            │ ⑤ 把 Dify 的回答填回去   │
            │                          │
            └────────────┬─────────────┘
                         │
                         │ HTTP 请求
                         │ 
                         │
                         │
                         ▼
            ┌──────────────────────────┐
              Dify               │
            │         ├── 多轮上下文   │
            │         ├── 大模型       │
            │         ├── Workflow     │
            │         └── Knowledge    │
            │                          │
            └──────────────────────────┘

 二、dify的接口文档
 > ## Documentation Index
> Fetch the complete documentation index at: https://docs.dify.ai/llms.txt
> Use this file to discover all available pages before exploring further.

# Workflow App API

> API for Workflow apps, covering workflow execution, run control and streaming, Human Input pauses, and run history

[Workflow apps](https://docs.dify.ai/en/cloud/use-dify/build/workflow-chatflow.md) run in `workflow` mode, executing the published workflow once per call and returning its outputs. There's no conversation state between calls, so each run is independent of any previous one.

> [!NOTE]
>
> Authentication, the base URL, and the `user` field that scopes end-user data are covered in [Get Started](https://docs.dify.ai/en/api-reference/guides/get-started.md) and [End User Identity](https://docs.dify.ai/en/api-reference/guides/end-user-identity.md).

## Run a Workflow

* **[Run Workflow](https://docs.dify.ai/en/api-reference/workflow-runs/run-workflow.md)**: execute the app's currently published workflow with the input variables you provide.

  Streaming mode delivers events live as nodes execute; blocking mode returns once the run finishes.
* **[Run Workflow by ID](https://docs.dify.ai/en/api-reference/workflow-runs/run-workflow-by-id.md)**: execute a specific published version instead, identified by the `workflow_id` from an earlier run response or run detail lookup.

## Follow and Control a Run

Whichever mode you run in, the response carries two IDs: `task_id` controls the run while it's active, and `workflow_run_id` identifies the persisted record afterward.

* **[Stop Workflow Task](https://docs.dify.ai/en/api-reference/workflow-runs/stop-workflow-task.md)**: cancel an active run by its `task_id`. Works in streaming mode, or after a blocking call returns with the run paused.
* **[Stream Workflow Events](https://docs.dify.ai/en/api-reference/workflow-runs/stream-workflow-events.md)**: resume a run's event stream from its `workflow_run_id` after a dropped connection or a pause.

  Pass `include_state_snapshot=true` to replay the status of already-executed nodes first, or `continue_on_pause=true` to stay open across multiple Human Input pauses.

## Pause for Human Input

When a run reaches a [Human Input node](https://docs.dify.ai/en/cloud/use-dify/nodes/human-input) whose delivery method is web app, complete the pause over the API:

**1. Listen for the pause**

The run's event stream emits `human_input_required` carrying a `form_token` and the run's `workflow_run_id`, then ends with the `workflow_paused` event.

**2. Fetch the form**

Load the form's contents with [Get Human Input Form](https://docs.dify.ai/en/api-reference/human-input/get-human-input-form.md), using the `form_token`.

**3. Submit the response**

Send the recipient's input with [Submit Human Input Form](https://docs.dify.ai/en/api-reference/human-input/submit-human-input-form.md). Submitting resumes the run.

**4. Resume the stream**

Open a new stream with [Stream Workflow Events](https://docs.dify.ai/en/api-reference/workflow-runs/stream-workflow-events.md), using the `workflow_run_id` from the paused stream, and follow the remaining events through to the run's finish.

For file-attached submissions and the full event sequence, see the [Human Input Flow](https://docs.dify.ai/en/api-reference/guides/human-input-flow.md) guide.

## Inspect Past Runs

* **[Get Workflow Run Detail](https://docs.dify.ai/en/api-reference/workflow-runs/get-workflow-run-detail.md)**: a run's status and outputs, by its `workflow_run_id`.
* **[List Workflow Logs](https://docs.dify.ai/en/api-reference/workflow-runs/list-workflow-logs.md)**: run-level summaries covering status, token usage, step count, and timing, rather than a node-by-node execution log; filter by keyword, status, date range, or who triggered the run.

For node-level events, stream the run instead of listing its logs: call [Run Workflow](https://docs.dify.ai/en/api-reference/workflow-runs/run-workflow.md) in streaming mode for a run you're starting, or [Stream Workflow Events](https://docs.dify.ai/en/api-reference/workflow-runs/stream-workflow-events.md) for one already in progress.

## Work with Files

* **[Upload File](https://docs.dify.ai/en/api-reference/files/upload-file.md)**: upload an image, document, audio, or video file, then pass the returned `id` as `upload_file_id` in a file-type input variable when you run the workflow. Files are scoped to the uploading end user.
* **[Get End User Info](https://docs.dify.ai/en/api-reference/end-users/get-end-user-info.md)**: look up an end user's details from an end-user ID, such as the `created_by` in the Upload File response.

## Transcribe and Synthesize Speech

* **[Convert Audio to Text](https://docs.dify.ai/en/api-reference/audio/convert-audio-to-text.md)**: transcribe an uploaded audio file (MP3, M4A, WAV, AMR, or MPGA, up to 30 MB) so end users can speak an input value instead of typing it.
* **[Convert Text to Audio](https://docs.dify.ai/en/api-reference/audio/convert-text-to-audio.md)**: synthesize a workflow's text output back into speech.

## Retrieve App Info and Settings

* **[Get App Info](https://docs.dify.ai/en/api-reference/applications/get-app-info.md)**: the app's name, description, tags, and mode.
* **[Get App Parameters](https://docs.dify.ai/en/api-reference/applications/get-app-parameters.md)**: the fields your calls send in `inputs` (names, types, defaults) plus the app's feature switches—the basis for building requests or a client UI.
* **[Get App Meta](https://docs.dify.ai/en/api-reference/applications/get-app-meta.md)**: tool icons and other configuration metadata.
* **[Get App WebApp Settings](https://docs.dify.ai/en/api-reference/applications/get-app-webapp-settings.md)**: the web app's site configuration, theme, and customization options.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.