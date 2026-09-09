
ЗАДАНИЕ ДЛЯ CLAUDE CODE — MONIK 3.2
Ты работаешь с репозиторием Monik_3.2.
Текущая версия создана как точная рабочая копия текущего состояния Monik 3.1 с VPS. Репозиторий является новой рабочей веткой разработки.
0. КРИТИЧЕСКИЕ ПРАВИЛА РАБОТЫ
Перед началом реализации:
Полностью проанализируй текущий код Monik_3.2.
Полностью изучи все архитектурные документы, технические документы и спецификации, находящиеся внутри репозитория.
Особое внимание удели документам, описывающим:
Level 1;
Level 2;
Opportunity;
Candidate;
Profit Calculator;
Resource Manager;
приоритеты;
агрегаторы;
scheduler;
notifications;
Telegram;
конфигурацию;
persistence/database;
production architecture.
Сопоставь документацию с фактической реализацией Monik 3.2.
Используй результаты предыдущего аудита текущей версии, описанные в moniklevel1audit.md.txt, как обязательную исходную информацию для этой задачи.
Сначала создай подробный поэтапный план реализации.
После создания плана самостоятельно реализуй все этапы последовательно до полного завершения задачи.
Не спрашивай у меня подтверждение между этапами.
Если в процессе обнаруживаются технические проблемы, анализируй их и самостоятельно выбирай корректное решение в рамках существующей архитектуры.
После завершения всех этапов выполни необходимые тесты, исправь найденные проблемы, сделай commit и push в Monik_3.2.
КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ЛИШНИЕ ИЗМЕНЕНИЯ
Не переписывай существующий код просто ради рефакторинга, улучшения стиля или потому, что тебе кажется, что другая архитектура лучше.
Изменять можно только то, что:
непосредственно связано с текущей проблемой;
необходимо для реализации указанной ниже логики очередей;
необходимо для исправления обнаруженных в аудите дефектов;
необходимо для корректной работы новых функций Telegram;
необходимо для реализации _MetricsStatsSource;
необходимо для механизма резервного копирования;
необходимо для соответствия версии приложения репозиторию.
Категорически НЕ изменяй без прямой технической необходимости:
архитектурные документы;
пользовательские настройки;
список токенов;
адреса токенов;
decimals токенов;
суммы сканирования;
интервал Level 1;
порог прибыльности;
существующую бизнес-логику Profit Calculator;
правила формирования Opportunity;
правила Level 1/Level 2, кроме части, необходимой для правильной приоритизации запросов;
существующие API-интеграции, если их изменение не требуется для решения текущей задачи;
Telegram secrets;
VPS production configuration;
любые unrelated modules.
Архитектурные документы являются источником требований, а не материалом для переписывания.
Если реализация требует расхождения с архитектурным документом, сначала проанализируй, действительно ли изменение необходимо. Не переписывай документ только для того, чтобы подогнать документацию под удобную реализацию.
1. ГЛАВНАЯ ПРОБЛЕМА — RATE LIMITING
Предыдущий аудит установил критическую проблему.
В конфигурации провайдеров уже существуют:
requests_per_second
max_concurrent_requests
Сейчас эти параметры не доходят до production Resource Manager.
В частности, аудит установил:
ProviderConfig.requests_per_second читается;
max_concurrent_requests читается;
ResourceManager создаётся без регистрации provider limits;
register_limits() фактически используется только в тестах;
_rate_limiter() поэтому не применяет provider-specific limiter;
provider-specific concurrency также не применяется;
в результате конфигурационные лимиты примерно 4.9 / 4.9 / 5.9 RPS фактически не работают;
реальная нагрузка доходила примерно до 17.2 RPS на провайдера;
это приводит к HTTP 429;
затем circuit breaker открывается;
после этого появляются массовые resource_circuit_open.
Необходимо исправить
Provider limits должны реально регистрироваться и применяться в production Resource Manager.
Используй существующий:
requests_per_second
как единственный источник истины для ограничения количества запросов провайдера.
Не создавай второй независимый параметр типа:
queue_rps
provider_queue_rps
rate_limit_rps
если он дублирует requests_per_second.
2. НОВАЯ АРХИТЕКТУРА ОЧЕРЕДЕЙ
Это основное архитектурное требование новой версии.
Нам нужны отдельные очереди запросов по агрегаторам.
Сейчас у нас три агрегатора:
Velora
0x
Uniswap
Следовательно, должно существовать три независимых request queue:
Resource Manager
│
├── Velora Queue
│
├── 0x Queue
│
└── Uniswap Queue
Архитектура должна быть динамической и модульной.
В будущем мы должны иметь возможность добавить новый агрегатор, например:
NewAggregator
без переписывания всей системы очередей.
То есть архитектура должна работать примерно по принципу:
provider_id → соответствующая provider queue
а не содержать жёстко зашитые:
if provider == velora
if provider == zero_x
if provider == uniswap
в десятках мест.
Добавление нового провайдера должно по возможности сводиться к регистрации его provider configuration / adapter / limits.
3. ЧЕТЫРЕ УРОВНЯ ПРИОРИТЕТА ВНУТРИ КАЖДОЙ ОЧЕРЕДИ
Каждый из трёх агрегаторов имеет свою очередь.
Внутри каждой очереди существуют четыре блока приоритета.
Приоритеты строго следующие:
Приоритет 1 — самый высокий
Level 2 scan, который начался раньше.
Приоритет 2
Level 2 scan, который начался позже.
Приоритет 3
Level 1 SELL.
Приоритет 4 — самый низкий
Level 1 BUY.
То есть:
1. Level2 — более ранний
2. Level2 — более поздний
3. Level1 SELL
4. Level1 BUY
Где:
1 = highest priority
4 = lowest priority
4. FIFO ВНУТРИ ОДНОГО ПРИОРИТЕТА
При одинаковом приоритете запросы должны выполняться по времени создания.
То есть:
priority → creation_time
Чем раньше запрос был создан, тем раньше он должен попасть на выполнение.
Пример:
Level1 BUY:
  request A created 10:00:01
  request B created 10:00:02
  request C created 10:00:03
Должно быть:
A → B → C
а не случайный порядок из-за asyncio.gather().
5. ОБЩАЯ МОДЕЛЬ REQUEST
Любой запрос, который требует обращения к внешнему provider API, должен проходить через соответствующую provider queue.
Это касается не только quote.
В частности:
price/quote request;
fee request;
любые другие provider HTTP/API requests;
Level 1;
Level 2;
maintenance/provider-related requests,
если они действительно обращаются к соответствующему aggregator API.
Логика:
Request created
       ↓
Определяется provider
       ↓
Попадает в queue этого provider
       ↓
Определяется priority
       ↓
Попадает в соответствующий priority block
       ↓
Внутри block сортируется по creation time
       ↓
Queue worker выбирает следующий request
       ↓
Проверяется provider rate limit
       ↓
HTTP request
6. RATE LIMIT ДЛЯ КАЖДОЙ ОЧЕРЕДИ
Каждая provider queue должна использовать тот же существующий параметр:
requests_per_second
Например:
Velora → 4.9 RPS
0x → 4.9 RPS
Uniswap → 5.9 RPS
Эти значения должны быть единственным источником ограничения.
Не создавать отдельную настройку queue rate.
Очень важно:
BUY и SELL одного провайдера должны использовать одну общую provider queue и один общий rate limit.
Нельзя сделать:
Uniswap BUY = 5.9 RPS
Uniswap SELL = 5.9 RPS
потому что это фактически даст:
11.8 RPS
вместо установленного:
5.9 RPS
То же самое относится к Level 1 / Level 2.
Все запросы одного provider должны делить общий provider-specific rate budget.
7. CONCURRENCY
Проанализируй существующий:
global semaphore;
Level1 semaphore;
Level2 semaphore;
Resource Manager gates;
provider max_concurrent_requests.
Исправь архитектуру так, чтобы provider concurrency действительно применялся там, где это предусмотрено текущей конфигурацией.
Не оставляй ситуацию, при которой:
ProviderConfig.max_concurrent_requests
существует, но фактически игнорируется.
При этом не создавай лишние уровни ограничения, если они не нужны.
Цель:
Provider Queue
   ↓
Provider rate limit
   ↓
Provider concurrency limit
   ↓
HTTP
с сохранением существующих глобальных защит там, где они архитектурно нужны.
8. CIRCUIT BREAKER — ИСПРАВИТЬ HALF_OPEN
Аудит обнаружил критический bug.
Текущая логика:
OPEN
 ↓
recovery timeout
 ↓
HALF_OPEN
 ↓
one probe
 ↓
success
 ↓
success count = 1
при:
half_open_max_calls = 1
success_threshold = 2
может оставить breaker навсегда в HALF_OPEN.
Это необходимо исправить.
Circuit breaker должен корректно проходить:
CLOSED
→ OPEN
→ HALF_OPEN
→ CLOSED
после успешного восстановления.
Не допускай состояния, в котором успешный recovery probe оставляет breaker permanently stuck в HALF_OPEN.
Добавь необходимые тесты на реальную последовательность:
failure → OPEN
recovery timeout → HALF_OPEN
request started
success
request started
success
→ CLOSED
и на соответствующие edge cases.
Если текущая комбинация:
half_open_max_calls
success_threshold
может создать невозможное состояние, добавь корректную валидацию конфигурации.
9. DATA ERRORS НЕ ДОЛЖНЫ ЛОМАТЬ CIRCUIT BREAKER
Аудит обнаружил ещё одну проблему.
Сейчас breaker.on_failure() вызывается для ошибок, которые не означают недоступность provider.
Например:
DataError;
validation errors;
unsupported;
другие неретраибельные ошибки.
В результате несколько случаев отсутствия ликвидности / некорректных данных могут открыть circuit breaker.
Это неправильно.
Используй существующую систему категорий retryability как source of truth.
Ошибки, которые не означают provider availability failure, не должны увеличивать failure counter circuit breaker.
Особенно:
DATA
VALIDATION
UNSUPPORTED
AUTHENTICATION
не должны автоматически считаться availability failure.
Сделай это согласованно с уже существующей моделью ошибок, не создавая параллельную систему классификации.
10. RETRY И CIRCUIT BREAKER
Аудит обнаружил ещё одну проблему.
Сейчас одна логическая операция может сделать:
attempt 1 → failure
attempt 2 → failure
attempt 3 → failure
и все три попытки увеличивают breaker failure counter.
Это приводит к слишком быстрому открытию breaker.
Нужно изменить семантику:
одна логическая операция после исчерпания retry budget = одна breaker failure.
То есть:
logical request
    attempt 1
    attempt 2
    attempt 3
        ↓
retry budget exhausted
        ↓
ONE circuit breaker failure
При этом retry должен продолжать нормально работать.
Не ломай существующий:
exponential backoff;
jitter;
Retry-After;
max attempts.
Сначала разберись в существующей реализации и измени только необходимую часть.
11. THUNDERING HERD / RATE LIMIT SMOOTHING
Аудит также отметил вторичную проблему:
после ожидания rate limiter несколько задач могут проснуться одновременно.
Проанализируй это.
Если после основной реализации provider queues сохраняется риск burst/thundering-herd, реализуй аккуратное сглаживание выдачи запросов.
Но:
не усложняй систему без необходимости.
Основная цель:
stable provider request rate
а не создание чрезмерно сложного scheduler.
12. LEVEL 1 И LEVEL 2
Сохрани существующую логику Level 1 и Level 2.
Но теперь их provider requests должны проходить через новую систему очередей.
Особенно важно:
Level 2
Level 2 должен иметь возможность вытеснять Level 1 согласно установленным приоритетам.
Если есть:
Level2 scan A
Level2 scan B
Level1 SELL
Level1 BUY
очередь должна выбирать:
A → B → SELL → BUY
Если появляется новый Level2 scan, он занимает место в Level2 priority block согласно времени создания.
13. ВАЖНАЯ ДЕТАЛЬ: ВРЕМЯ LEVEL 2
Приоритет Level2 определяется не просто тем, что это Level2.
Нужно учитывать время начала Level2 scan.
Например:
Level2 A started 12:00:01
Level2 B started 12:00:05
A должен иметь более высокий приоритет:
A → B
Таким образом Level2 priority фактически является:
(priority class = LEVEL2, creation/start time)
и более ранний Level2 scan должен обслуживаться раньше более позднего.
14. TELEGRAM НЕ СМЕШИВАТЬ С PROVIDER QUEUES
Telegram notification system не является частью этих трёх provider queues.
У Telegram должна остаться собственная очередь.
Правило:
Чем раньше сформировалось Telegram notification, тем раньше оно должно отправляться.
То есть:
Telegram notification queue
FIFO by creation time
Provider queue и Telegram queue не должны блокировать друг друга.
Не включай Telegram requests в:
Velora Queue
0x Queue
Uniswap Queue
15. TELEGRAM COMMANDS
Нужно сделать удобное управление Monik через Telegram.
Минимальный набор кнопок:
Запустить
Остановить
Перезапустить
Статус
Статус агрегатора
Для Статус агрегатора желательно сделать выбор:
Velora
0x
Uniswap
Также предложи и реализуй другие действительно полезные функции для неопытного пользователя.
Например, проанализируй необходимость:
Помощь
Список команд
Статистика
Статус Level 1
Статус Level 2
Последний scan
Последние ошибки
Последние opportunities
Настройки
Но не добавляй функции только ради количества.
Каждая Telegram функция должна иметь практический смысл.
16. СОЗДАТЬ ПОЛНЫЙ СПИСОК TELEGRAM COMMANDS
В репозитории должен существовать отдельный понятный документ со всеми доступными Telegram commands.
Например:
docs/telegram_commands.md
Название выбери в соответствии с существующей структурой документации репозитория.
Документ должен содержать:
command;
назначение;
что возвращает;
ограничения;
какие кнопки соответствуют command;
какие команды доступны обычному пользователю;
какие действия требуют подтверждения.
Не ломай существующую документацию.
Если аналогичный документ уже существует — расширь его, а не создавай дубликат.
17. TELEGRAM STATUS
Статус должен быть полезным неопытному пользователю.
Проанализируй и при необходимости добавь информацию:
Application version
Environment
Scanner status
Level1 status
Level2 status
Last scan
Next scan
Provider status
Provider queue status
Rate limit configuration
Current errors
Database status
Telegram status
Но не показывай секреты.
Никогда не показывай:
API keys;
bot token;
private keys;
sensitive environment values.
18. ВЕРСИЯ ПРИЛОЖЕНИЯ
Сейчас аудит/production logs показали:
application version: 0.1.0
environment: development
при том, что рабочий репозиторий называется:
Monik_3.2
Это необходимо исправить.
Версия приложения должна соответствовать текущей рабочей версии:
3.2
или существующему принятому в проекте формату, например:
Monik 3.2
Выбери единый формат после анализа существующей системы version metadata.
Одна и та же версия должна использоваться во всех местах, где приложение сообщает свою версию, включая:
startup logs;
Telegram /status;
Telegram startup notification;
Telegram notification после restart;
health notifications;
diagnostics;
CLI, если версия там отображается.
После перезагрузки приложения Telegram должен сообщать именно актуальную версию Monik 3.2, а не 0.1.0.
Не делай отдельные hardcoded версии в разных местах.
Должен существовать один source of truth для application version.
19. ENVIRONMENT
Проанализируй текущий:
environment: development
Определи, почему production application на VPS показывает development.
Исправь это только в рамках данной задачи, чтобы production deployment корректно идентифицировался как production, если архитектура проекта это предусматривает.
Не меняй production deployment механически и не создавай новую систему environment configuration без необходимости.
20. _MetricsStatsSource
В аудите был обнаружен конкретный дефект:
_MetricsStatsSource
создаётся в container.py, но фактически не получает актуальные значения metrics.
В результате Telegram /stats может показывать нулевые значения:
cycles = 0
opportunities = 0
sent notifications = 0
даже когда приложение реально работает.
Необходимо:
Найти текущую реализацию _MetricsStatsSource.
Понять существующую metrics architecture.
Подключить источник к фактическим metrics/events приложения.
Исправить /stats, чтобы он отражал реальные данные.
Не создавай вторую независимую metrics system.
Используй существующую metrics infrastructure.
21. МЕХАНИЗМ РЕЗЕРВНОГО КОПИРОВАНИЯ
Добавь механизм резервного копирования.
Цель:
сохранность важных данных без накопления лишнего мусора.
По умолчанию:
раз в неделю
суббота
03:00
Механизм должен быть экономным по месту.
Не нужно создавать полные копии всего проекта.
Определи, какие данные действительно необходимо сохранять.
В первую очередь проанализируй:
SQLite database;
необходимые persistent state;
важные runtime data, если они действительно нужны для восстановления.
Не копируй:
.venv;
Git repository;
cache;
временные файлы;
логи, если они не нужны для восстановления;
секреты;
лишние runtime artifacts.
Сделай backup rotation/retention, чтобы старые копии автоматически удалялись и место не заполнялось бесконечно.
Периодичность по умолчанию:
Saturday 03:00
Сделай механизм конфигурируемым через существующую configuration architecture, но не изменяй пользовательские настройки сканирования.
Добавь:
backup success/failure logging;
проверку существования backup;
безопасное создание backup;
защиту от повреждения DB;
понятный статус backup в diagnostics/Telegram, если это соответствует существующей архитектуре.
22. RESOURCE MANAGER — СОХРАНИТЬ ЕГО РОЛЬ
Не удаляй Resource Manager.
Он должен остаться центральным механизмом управления внешними ресурсами.
Итоговая логика должна быть примерно:
Request
   ↓
Resource Manager
   ↓
Provider Queue
   ↓
Priority selection
   ↓
FIFO within priority
   ↓
Rate limiter
   ↓
Concurrency control
   ↓
Circuit breaker
   ↓
Retry/backoff where applicable
   ↓
Provider API
Но перед реализацией внимательно сопоставь эту схему с существующим кодом.
Не создавай параллельный Resource Manager.
Не создавай вторую систему retry.
Не создавай вторую систему circuit breaker.
Не создавай вторую систему rate limiting.
23. ДОБАВЛЕНИЕ НОВОГО АГРЕГАТОРА
Архитектура provider queues должна быть расширяемой.
Сейчас:
Velora
0x
Uniswap
В будущем:
Velora
0x
Uniswap
NewAggregator
Добавление нового aggregator должно автоматически создавать/регистрировать соответствующую очередь на основании provider configuration.
Не должно требоваться переписывать core queue architecture.
Провайдер должен передавать в систему как минимум:
provider_id
requests_per_second
max_concurrent_requests
и использовать существующий механизм регистрации provider.
24. НЕ ИЗМЕНЯТЬ ПРОФИТ-КАЛЬКУЛЯТОР
Profit Calculator остаётся единственным владельцем финансовых формул.
Не переносить расчёты прибыли в queue/resource/provider layer.
Не дублировать:
fees;
gas;
slippage;
net profit;
ROI.
Queue должна решать:
когда и какой запрос отправить
а не:
какова прибыль opportunity
25. НЕ МЕНЯТЬ ТЕКУЩИЕ ПОЛЬЗОВАТЕЛЬСКИЕ ПАРАМЕТРЫ
Без прямой необходимости НЕ менять:
30-token universe
50 USDT
100 USDT
500 USDT
1000 USDT
Level1 interval = 600 sec
Level2 enabled
net ROI threshold = 1.00%
Не уменьшать universe.
Не уменьшать amounts.
Не увеличивать interval.
Не снижать threshold.
Не отключать Level2.
Проблема должна решаться архитектурой управления запросами, а не искусственным уменьшением нагрузки.
26. ТЕСТЫ — ОБЯЗАТЕЛЬНО
Сначала изучи существующую тестовую структуру.
Добавь тесты для:
Provider limits
Проверить:
ProviderConfig.requests_per_second
        ↓
Resource Manager
        ↓
Provider Queue
        ↓
RateLimiter
Provider isolation
Проверить, что:
Velora traffic
не расходует лимит:
Uniswap
и наоборот.
Shared provider budget
Проверить, что:
BUY + SELL + Level2
одного provider используют один общий rate budget.
Priority
Проверить:
Level2 older
Level2 newer
Level1 SELL
Level1 BUY
именно в таком порядке.
FIFO
Внутри одного priority:
older request → newer request
Circuit breaker
Проверить полный lifecycle:
CLOSED → OPEN → HALF_OPEN → CLOSED
Data errors
Проверить, что DataError не открывает availability breaker.
Retry
Проверить:
3 failed attempts
=
1 logical breaker failure
Metrics
Проверить, что реальные scan events отражаются в /stats.
Telegram
Проверить commands, status, provider status и version.
Backup
Проверить:
создание;
целостность;
retention;
расписание;
отсутствие secrets/лишних файлов.
Full suite
После targeted tests выполнить полный существующий test suite.
Не удаляй существующие тесты только потому, что они больше не подходят. Если тест действительно проверяет старое неправильное поведение — исправь его так, чтобы он проверял новое требование.
27. COMPONENT / LOAD TEST
Создай/обнови component tests для сценария:
3 providers
29 intermediate tokens
4 amounts
Level1 BUY/SELL
Level2
Проверь:
provider isolation;
priority;
FIFO;
RPS;
concurrency;
retry;
circuit breaker;
отсутствие массового resource_
