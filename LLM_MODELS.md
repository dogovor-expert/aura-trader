# Облачные LLM для агента: какие нужны и сколько стоят

## Короткий ответ
Нужно **1-2 модели**, не больше:
- **L1 Scout (каждый цикл, 288 раз/сутки)** — быстрая дешёвая, JSON-вердикт.
- **L2 Judge (только сильные сигналы, ~5-10% циклов)** — умная, подтверждает/отменяет L1.

Третья модель (риск-менеджер) **не окупается** — риск режется кодом (порог confidence, cooldown, kill-switch).

## Текущая конфигурация (работает)
| Роль | Провайдер и модель | Статус |
|------|--------|--------|
| **L1 Scout** | anymodel.org `cx/gpt-6-luna` | ✅ работает, JSON валиден |
| **L2 Judge** | anymodel.org `cc/claude-haiku-4-5-20251001` | ✅ работает, вызывается при conf L1 ≥ 0.65 |
| Ключ | `ANYMODEL_API_KEY` (переменная окружения пользователя) | активен |

Провайдер `anymodel` добавлен в `src/llm.py` как OpenAI-совместимый
(`https://anymodel.org/v1/chat/completions`). Модели в нём ~109, в том числе
`cx/*` (gpt-5.5/5.6/6/6.1 — astra, luna, sol, terra) и `cc/*` (claude haiku/opus/sonnet/fable).
Разбор без ключа на диске: `check_anymodel.py <model>`.

> Если anymodel недоступен или закончится лимит — рабочий бесплатный запасной вариант:
> Groq `openai/gpt-oss-120b` (L1) + `qwen/qwen3.8-27b` (L2), оба дают $0.

## Запасные варианты и состояние ключей
| Роль | Модель | Статус | Цена |
|------|--------|--------|------|
| L1 запасной | Groq `openai/gpt-oss-120b` | РАБОТАЕТ, JSON OK | $0 (free tier) |
| L2 запасной | Groq `qwen/qwen3.8-27b` | РАБОТАЕТ | $0 |
| Ключи в системе | `ANYMODEL_API_KEY` | активен | — |
| | `GROQ_API_KEY` | активен | — |
| | `OPENAI_API_KEY` | НЕВАЛИДЕН (401) | нужен новый |
| | `DEEPSEEK_API_KEY` | баланс $0.00 | пополнить |
| | `ANTHROPIC_API_KEY` | нет | получить |
| | `GEMINI_API_KEY` | нет | получить |

## Рекомендуемый рост
1. **Сейчас (MVP, $0/мес):** L1 = Groq gpt-oss-120b. Уже включено в config.json. Paper 2 недели.
2. **Апгрейд L1 ($3.5/мес):** если precision Groq <60% → `openai/gpt-4o-mini` ($0.15/$0.60 за 1M) или `gemini-2.5-flash-lite` ($0.10/$0.40). Расчёт: 288 циклов × 30 дней × (1500 in + 300 out) ≈ 13M in + 2.6M out ≈ $2 + $1.5.
3. **Добавить L2-судью (~+$5/мес):** `anthropic claude-sonnet-4-5` или `openai/gpt-4o`, вызывать только при confidence L1 ≥0.65 или эвристической дивергенции (~20 раз/сутки). Включается в config.json: `l2_enabled=true`.
4. **Максимум ($8-12/мес):** L1 gpt-4o-mini + L2 sonnet. Больше не надо.

## Почему не reasoning-модели (o3, R1) в цикл
Thinking-токены 1000-3000 раздувают ответ до 8-20с — не укладываемся в 5-мин цикл с запасом на сеть через прокси. Только L2 по требованию, если вообще.

## Защита от галлюцинаций (уже в коде src/llm.py)
- Strict JSON + temperature=0 + max 400 токенов.
- Промпт требует `null_hypothesis` («почему это шум»).
- Trade gate: `confidence ≥ 0.75`, иначе WAIT.
- L2-судья подтверждает сильные сигналы.
- Лог всех вердиктов в signals.log для калибровки порога.

## Как переключить модель
1. Ключ anymodel лежит в `ANYMODEL_API_KEY` (переменная окружения пользователя).
2. В `config/config.json` → блок `llm`:
   - `provider`: `anymodel` | `groq` | `openai` | `anthropic` | `deepseek` | `gemini` | `openrouter` | `none`;
   - `l1_model`, `l2_model`: идентификаторы вида `cx/gpt-6-luna`, `cc/claude-haiku-4-5-20251001`;
   - `l2_enabled`, `l2_trigger_conf`, `trade_conf`.
3. Проверка модели: `venv\Scripts\python src\check_anymodel.py cx/gpt-6-luna`.
4. Разовая проверка всего каскада: `venv\Scripts\python src\main.py --once`.
5. Постоянный цикл: `run-oracle.cmd` (читает `ANYMODEL_API_KEY` из окружения).

Полный список моделей провайдера сохранён в
`C:\Users\alikpc\.agents\team\work\anymodel-models.json` (109 шт., включая генерацию картинок — она для торговли не нужна).

## Прокси
Все LLM-вызовы идут через SOCKS5 из config.json → proxy.url (до 27.10.26).
Проверено: OpenAI 401 (достижим), Anthropic 405/401 (достижим), DeepSeek 401/402 (достижим), Gemini 403 (нужен ключ для проверки), Groq 200 (работает).
Bybit через этот прокси НЕ работает (US IP забанен) — нужен не-US прокси для live-торговли.
