@echo off
REM API keys for LLM cascade. Fill in and run: set_keys.bat && python src\main.py --once
REM Get keys: OpenAI https://platform.openai.com/api-keys
REM Anthropic https://console.anthropic.com/settings/keys
REM DeepSeek https://platform.deepseek.com/api_keys
REM Gemini https://aistudio.google.com/apikey
REM OpenRouter https://openrouter.ai/keys

REM set OPENAI_API_KEY=sk-...
REM set ANTHROPIC_API_KEY=sk-ant-...
REM set DEEPSEEK_API_KEY=sk-...
REM set GEMINI_API_KEY=AIza...
REM set OPENROUTER_API_KEY=sk-or-...

echo Edit this file and uncomment your keys, then re-run it.
echo Current status:
if defined OPENAI_API_KEY (echo OPENAI_API_KEY=set) else (echo OPENAI_API_KEY=empty)
if defined ANTHROPIC_API_KEY (echo ANTHROPIC_API_KEY=set) else (echo ANTHROPIC_API_KEY=empty)
if defined DEEPSEEK_API_KEY (echo DEEPSEEK_API_KEY=set) else (echo DEEPSEEK_API_KEY=empty)
if defined GEMINI_API_KEY (echo GEMINI_API_KEY=set) else (echo GEMINI_API_KEY=empty)
if defined OPENROUTER_API_KEY (echo OPENROUTER_API_KEY=set) else (echo OPENROUTER_API_KEY=empty)
