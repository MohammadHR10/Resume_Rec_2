# SIS fastLLM gateway: server-side fixes

A runbook for the administrator of `https://llmgateway.utshare.utsystem.edu:7008`.
Three changes, in priority order. Item 1 is a real defect; items 2 and 3 are papercuts
that made it hard to find.

What this app currently works around in `backend/llm/litellm.py`, and what would have
to change on the SIS side to let us delete that code.

**Endpoint:** `https://llmgateway.utshare.utsystem.edu:7008`
**Model group:** `GPT 120b` → `openai/gpt-oss-120b`
**Stack:** litellm proxy in front of vLLM 0.21.0 (`system_fingerprint: vllm-0.21.0-ad7fb20b`)

## Before you start

You will need shell access to the vLLM host, edit access to the litellm `config.yaml`,
and the ability to restart both. Set these for the commands below — `$GATEWAY` is the
public proxy, `$VLLM` is the vLLM backend reached directly, bypassing litellm.

```bash
export GATEWAY=https://llmgateway.utshare.utsystem.edu:7008
export VLLM=http://<vllm-host>:<vllm-port>     # the backend litellm routes to
export KEY=<a gateway api key>
```

**Step 0. Capture a baseline so you can tell whether anything changed.**

```bash
curl -sS $GATEWAY/v1/chat/completions \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"GPT 120b","seed":42,
       "messages":[{"role":"system","content":"Answer using a tool."},
                   {"role":"user","content":"Say hello."}]}' | tee baseline.json
```

Keep `baseline.json`. You will diff against it after each change.

## 1. vLLM is not parsing harmony tool calls

### The symptom

GPT-OSS models speak the harmony response format. When one decides to call a tool it
ends its turn on the harmony `<|call|>` token. With no tool-call parser configured,
vLLM cannot turn that into an OpenAI `tool_calls` array, so it drops the call and
returns a *successful* 200 containing:

- `choices[0].message.content: null`
- no `tool_calls` key at all
- `reasoning_content` populated with the model's analysis
- `finish_reason: "stop"`
- `provider_specific_fields.stop_reason: 200012` — the harmony `<|call|>` token id, and
  the only field distinguishing this from a model that genuinely had nothing to say

When the partial parse fails more loudly, the same request returns instead:

```
HTTP 500  litellm.InternalServerError: OpenAIException -
unexpected tokens remaining in message header: Some("to=tool")
```

Measured against this app's chat system prompt, 2 replies in 12 came back with any
content at all. Declaring tools natively in the request did not help — 12 in 12 empty,
never a `tool_calls` key.

### Do this

**Step 1.1. Find how vLLM is launched** — the systemd unit, container command, or
whatever starts it.

```bash
systemctl cat vllm 2>/dev/null || ps -ef | grep '[v]llm serve'
```

**Step 1.2. Check whether the parser flags are already there.**

```bash
ps -ef | grep '[v]llm serve' | tr ' ' '\n' | grep -E 'tool-call-parser|reasoning-parser|auto-tool'
```

If that prints nothing, the flags are absent and this is your defect. Continue.
If the flags are present, skip to step 1.6 — the parser is configured but not working,
and the verification there will tell you how it is failing.

**Step 1.3. List the parser names this build actually accepts.** Do not guess them from
this document; the valid choices vary by version.

```bash
vllm serve --help | grep -A20 'tool-call-parser'
vllm serve --help | grep -A20 'reasoning-parser'
```

Pick the choice for gpt-oss / harmony from each list. On builds that ship it these are
usually named `openai` for the tool-call parser and `openai_gptoss` for the reasoning
parser, but take the names from the help output, not from here.

**Step 1.4. If neither list contains a gpt-oss or harmony option,** this build predates
the support and no flag will fix it. Stop, upgrade vLLM to a version whose help lists
one, then resume at step 1.3.

**Step 1.5. Add the flags and restart.**

```bash
vllm serve openai/gpt-oss-120b \
  --enable-auto-tool-choice \
  --tool-call-parser <name from step 1.3> \
  --reasoning-parser <name from step 1.3> \
  ...your existing flags...
```

```bash
systemctl restart vllm && systemctl status vllm --no-pager
```

If the service fails to start, the parser name is wrong for this build. Return to step
1.3 and take the name verbatim from the help output.

**Step 1.6. Verify against vLLM directly, with a tool declared.**

```bash
curl -sS $VLLM/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"openai/gpt-oss-120b",
       "messages":[{"role":"user","content":"What is the weather in Austin?"}],
       "tools":[{"type":"function","function":{"name":"get_weather",
         "parameters":{"type":"object","properties":{"city":{"type":"string"}},
                       "required":["city"]}}}]}' | jq '.choices[0].message'
```

If you see a populated `tool_calls` array, item 1 is fixed. Go to step 1.7.
If `content` is still `null` with no `tool_calls`, the parser is not engaging — check
the vLLM startup log for the line naming the parser it loaded and confirm it matches
what you passed.
If the request 500s with `unexpected tokens remaining in message header`, the parser is
loaded but does not understand this model's output; you need a newer vLLM.

**Step 1.7. Repeat the same request through the proxy** (`$GATEWAY`, model `GPT 120b`,
with the authorization header). It must also return `tool_calls`. If vLLM returns them
and the proxy does not, litellm is stripping them — confirm the deployment's
`litellm_params.model` carries an `openai/` prefix so litellm treats it as an
OpenAI-compatible chat model rather than a text-completion one.

**Step 1.8. Tell us it is done.** This change is the one the client-side workaround
exists for, and we remove that code once you confirm.

## 2. `response_format: json_schema` is accepted and silently ignored

### The symptom

The gateway returns 200 for a strict `json_schema` response format and then does not
enforce it. The same prompt sent with and without the parameter returns byte-identical
replies, neither of which matches the schema.

This is worse than not supporting it. Our client already falls back to prompt-instructed
JSON when a gateway *rejects* `response_format`, but that path is keyed on a 400, so it
never fires here. The schema looks enforced to every caller and is doing nothing.

**We do not need this parameter honoured. We need it to stop lying.** Honouring it and
rejecting it are equally acceptable outcomes; silence is the only unacceptable one.

### Do this

**Step 2.1. Determine which layer is eating it.** Send the same schema straight to vLLM,
bypassing litellm.

```bash
curl -sS $VLLM/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"openai/gpt-oss-120b",
       "messages":[{"role":"user","content":"Give me a person."}],
       "response_format":{"type":"json_schema","json_schema":{"name":"p","strict":true,
         "schema":{"type":"object","additionalProperties":false,
           "required":["name","age"],
           "properties":{"name":{"type":"string"},"age":{"type":"integer"}}}}}}'
```

If the reply is a JSON object with exactly `name` and `age`, vLLM is fine and litellm is
the culprit — go to step 2.3.
If the reply is prose, or JSON of a different shape, vLLM is the culprit — go to step 2.2.

**Step 2.2. Fix vLLM.** Its OpenAI server does support `response_format: json_schema`,
routing it to the structured-outputs backend (xgrammar by default, with outlines and
llguidance as alternates).

First confirm a backend is available in this build:

```bash
vllm serve --help | grep -iE 'structured-outputs|guided-decoding'
```

If a backend flag exists, set it explicitly (`--guided-decoding-backend xgrammar` on
older builds, `--structured-outputs-config` on newer ones), restart, and re-run step 2.1.

If it still does nothing, suspect a conflict with harmony parsing: constrained decoding
and channel parsing both want to control token selection, and the grammar has to apply
to the final channel only, not the analysis channel the model reasons in. Completing
item 1 first — specifically the reasoning parser — is a prerequisite for that separation.
If item 1 is already done and step 2.1 still ignores the schema, this build cannot
combine the two for this model: go to step 2.4 and make it fail loudly instead.

**Step 2.3. Fix litellm.** Two things, in this order.

First, check `drop_params`. When it is true, litellm silently strips any parameter the
model is not registered as supporting, which produces exactly this symptom — a clean
200 with the constraint gone.

```bash
grep -rn 'drop_params' /path/to/litellm/config.yaml
```

If it is `true` globally, that is your cause. Do not simply leave it on; see step 3.2.

Second, mark the deployment as supporting response schemas so litellm passes the
parameter through to vLLM rather than transforming or discarding it:

```yaml
model_list:
  - model_name: GPT 120b
    litellm_params:
      model: openai/gpt-oss-120b
      api_base: http://<vllm-host>:<port>/v1
    model_info:
      supports_response_schema: true
```

Restart litellm and re-run step 2.1 through `$GATEWAY`.

Optionally, if you want a server-side safety net on top, set
`litellm_settings.enable_json_schema_validation: true`. The proxy then validates the
reply against the supplied schema and retries on a miss. It costs latency, but it covers
models that cannot be constrained at decode time.

**Step 2.4. If neither layer can honour it, make it reject loudly instead.** This is a
fully acceptable outcome and takes no work: leave the model *unmarked* and `drop_params`
false, and litellm raises `UnsupportedParamsError` on its own. Our client watches for
that 400 and falls back to putting the schema in the prompt.

**Step 2.5. Verify.** The test is that the parameter changes the output. Send the same
prompt with and without `response_format` and compare. Today the two replies are
byte-identical — that is the whole tell. After the fix they must differ, or the request
carrying the parameter must return 400.

## 3. `reasoning_effort` is rejected outright

### The symptom

```
HTTP 400  litellm.UnsupportedParamsError: openai does not support parameters:
['reasoning_effort'], for model=openai/gpt-oss-120b
```

The model supports it; the litellm routing config does not pass it through.

### Do this

**Step 3.1. Add explicit support** on the deployment so the parameter reaches vLLM,
alongside the change from step 2.3:

```yaml
    model_info:
      supports_reasoning: true
      supports_response_schema: true
```

**Step 3.2. Do NOT fix this by setting `drop_params: true` globally.** That is the same
switch that silently strips unsupported parameters, so it would trade this loud, useful
failure for the quiet one described in item 2 — and apply it to every other parameter
besides. If you must use it, scope it to `reasoning_effort` alone on this deployment.

**Step 3.3. Verify.**

```bash
curl -sS $GATEWAY/v1/chat/completions \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"GPT 120b","reasoning_effort":"low",
       "messages":[{"role":"user","content":"Say hello."}]}' | jq '.choices[0].message'
```

A 200 is a pass. A 400 still naming `reasoning_effort` means step 3.1 did not take —
confirm you edited the deployment litellm actually routes `GPT 120b` to.

## What to send back

For each item: whether it is done, and the output of its verification step. Item 1 is
the one that matters — tell us as soon as it lands, even if 2 and 3 are still open.
