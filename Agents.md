# AGENTS.md — инструкции для AI-агентов проекта Нейроныч

Этот файл обязателен для всех AI-агентов, которые читают, меняют, тестируют или развивают этот репозиторий.

## 1. Обязательное использование `.agents/`

В репозитории подключена библиотека skills в `.agents/`.

**Для любой нетривиальной задачи агент обязан использовать релевантные skills из `.agents/skills/`.**

Перед началом работы:

1. Убедиться, что submodule инициализирован:
   ```bash
   git submodule update --init --recursive
   ```
2. Прочитать этот `AGENTS.md`.
3. Прочитать проектные документы, относящиеся к задаче.
4. Найти релевантные skills через:
   - `.agents/SKILLS_INDEX.md`;
   - поиск по `.agents/skills/*/SKILL.md`.
5. Выбрать **минимальный достаточный набор** skills.
6. Полностью прочитать `SKILL.md` каждого выбранного skill.
7. Загружать `references/`, `scripts/` и `assets/` только если это требуется самим skill.
8. Следовать skill во время реализации и проверки.
9. В итоговом отчёте перечислить skills, которые реально повлияли на решение.

Не допускается:
- игнорировать `.agents/` для нетривиальной задачи;
- загружать весь каталог skills в контекст;
- запускать скрипты из skill без предварительного просмотра;
- ссылаться на skill, который фактически не использовался.

Если submodule `.agents/` недоступен или не инициализируется, агент должен явно остановиться и сообщить об этом до существенных изменений.

## 2. Приоритет инструкций

При конфликте использовать следующий порядок:

1. прямое текущее указание пользователя;
2. этот `AGENTS.md`;
3. актуальная продуктовая спецификация;
4. project-local conventions и существующий код;
5. инструкции выбранных skills;
6. общие инженерные практики.

Skill — это рабочая методика, а не разрешение нарушать требования проекта.

## 3. Источники продуктовой истины

Перед изменением поведения продукта проверить:

- `PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md` — основной набор подтверждённых продуктовых решений;
- `IMPLEMENTATION_EPICS.md` — декомпозиция реализации;
- `PRODUCTION_LAUNCH_CHECKLIST.md` — launch gates и требования к готовности.

Если документы противоречат друг другу, приоритет у более свежего явного решения в product brief. Не придумывать продуктовые правила без основания.

## 4. Базовая архитектура проекта

Целевой стек:

- Python
- FastAPI
- aiogram
- PostgreSQL
- Redis
- SQLAlchemy 2
- Alembic
- Pydantic Settings
- httpx
- Docker Compose
- Nginx
- MinIO
- Prometheus-compatible metrics
- structured JSON logs

Финансовые и durable-состояния должны иметь PostgreSQL как источник истины. Redis не должен быть единственным местом хранения критичных данных.

## 5. Финансовая точность

Для денег запрещён binary float.

Использовать:
- PostgreSQL `NUMERIC`;
- Python `Decimal`;
- high precision для промежуточных вычислений;
- финальный partner charge округлять `ROUND_HALF_UP` до `0.01 RUB`.

Исторические финансовые операции и snapshots не пересчитывать задним числом.

Денежные ledger-операции должны быть append-only либо корректироваться отдельными компенсирующими записями.

## 6. Provider abstraction

Ядро не должно быть жёстко привязано к ArgoLink.

ArgoLink — первый provider adapter.

Provider-specific:
- keys;
- task IDs;
- raw errors;
- upstream costs;
- routing reasons

не должны утекать в partner-facing API.

Любой fallback допустим только при эквивалентной поддержке исходного request без silent degradation.

## 7. Idempotency и side effects

Все внешние side effects проектировать как идемпотентные или защищённые от повторного применения.

Особенно:
- generation creation;
- balance settlement;
- payment credit;
- refund/reversal;
- webhook delivery;
- provider submit;
- manual admin actions.

Повтор запроса не должен приводить к duplicate provider job, double charge или double credit.

## 8. Безопасность

Никогда не коммитить:
- API keys;
- provider keys;
- Telegram bot tokens;
- webhook secrets;
- private keys;
- production credentials;
- recovery secrets.

Логи не должны содержать plaintext secrets.

При работе с webhook URLs учитывать SSRF:
- блокировать loopback;
- private networks;
- link-local;
- metadata endpoints;
- небезопасные redirect chains.

При работе с партнёрами всегда проверять tenant/account isolation.

## 9. Git workflow

Кодовые изменения не пушить напрямую в `main`.

Стандартный процесс:
1. создать отдельную branch;
2. внести минимально необходимое изменение;
3. запустить релевантные tests/checks;
4. открыть PR;
5. убедиться, что CI зелёный;
6. merge только после прохождения обязательных checks.

Не смешивать несвязанные изменения в одном PR.

## 10. Definition of Done

Задача не считается завершённой только потому, что код написан.

Перед завершением агент обязан:

- проверить acceptance criteria;
- запустить релевантные unit/integration tests;
- проверить migrations, если менялась БД;
- проверить error paths;
- проверить idempotency для денежных/внешних операций;
- проверить security implications;
- проверить отсутствие утечек secrets;
- проверить backward compatibility API, если затрагивался контракт;
- обновить docs/spec при изменении подтверждённого поведения.

Для критичных денежных и generation flow задач обязательно проверить сценарии повторов, падений и restart/retry.

## 11. Рекомендуемая маршрутизация skills

Это не закрытый список. Всегда искать точные skills в `.agents/SKILLS_INDEX.md`.

Ориентиры:

- FastAPI / API: искать `fastapi`, `api`, `backend`
- PostgreSQL / migrations: `postgres`, `database`, `migration`
- Redis / queues: `redis`, `queue`, `async`
- Telegram / aiogram: `telegram`, `bot`
- security: `security`, `api-security`, `threat`, `secrets`
- testing: `pytest`, `testing`, `tdd`, `integration`
- CI/CD: `github`, `ci-cd`, `docker`, `deploy`
- observability: `prometheus`, `slo`, `observability`
- architecture: `architecture`, `ddd`, `backend-architect`
- production readiness: `production-audit`, `pre-ship-gate`, `verification-before-completion`

## 12. Работа с неопределённостью

Если решение уже определено в product brief — не задавать его заново.

Если не хватает технического факта:
- сначала проверить код;
- затем документацию provider/vendor;
- затем сформулировать минимальное обоснованное решение.

Не превращать технические детали, которые можно безопасно решить инженерно, в бесконечный продуктовый опрос.

Если неопределённость влияет на деньги, безопасность, юридические обязательства или необратимую архитектуру — явно вынести её до реализации.
