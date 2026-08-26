# Keystone — documented entry points.
#
# Everything here runs from the repository root. `make help` lists the targets;
# the clean-clone order is:
#
#     cp .env.example .env && make up && make migrate && make seed
#     make serve            # terminal 1
#     make sync             # terminal 2 — loads the database and DETECTS (~1 min+)
#     make reconcile        # terminal 2 — turns conflicts into proposals (seconds)
#     make suite            # the graded scorecard (~35 min — see below)
#
# `make suite` IS THE SLOW STEP, and one row is why: `coverage` shells out to a
# real pytest run rather than trusting a stored number, and the committed
# `docs/scorecard.txt` clocks that child at 32m29s (4,771 passed, 2 skipped).
# `KEYSTONE_COVERAGE_TIMEOUT` (default 2400s) kills it and turns the row RED, so
# the headroom is roughly seven minutes — raise it before adding tests.
#
# `make reconcile` IS PART OF THE PATH, not an extra. `sync` ends at detection
# (`SYNC_STAGES` = ingest, materialize, invariants), so it leaves `conflicts`
# populated and `proposals` EMPTY. Nothing else in this file fires
# `POST /internal/reconcile`, so a run that stops at `sync` shows a dashboard
# with conflicts and zero proposals — which reads as if the guarded-automation
# half of the system were not built.
#
# `make env` prints the configuration the service will actually load, secrets
# redacted — run it first when anything looks unconfigured.
#
# Both triggers are idempotent per run id, and the claim is keyed on the job as
# well as the id (`recon.api.internal.claim_run`), so ONE `RUN_ID` covers both:
# `make sync` and `make reconcile` under `grader-001` each claim once. Firing
# either one twice under the same id answers `"status":"replayed"` and does not
# run again; to run a second time deliberately, give it a fresh id —
# `make sync RUN_ID=grader-002`, `make reconcile RUN_ID=grader-002`.
#
# ---------------------------------------------------------------------------
# `.env` — the repo-root file `cp .env.example .env` creates configures EVERY
# target below. It used to configure none of them: each recipe runs with the
# working directory set to `service/` or `dashboard/` (see UV and PNPM), and
# both pydantic-settings and Vite resolve their env files against that working
# directory, so the repo-root `.env` was opened by nobody. `make serve` then
# came up looking healthy and answered 401 on the trigger the operator had just
# configured.
#
# `$(DOTENV)` prefixes the recipes that need configuration and exports the file
# into their environment. That is the half of the fix `recon.config` cannot do:
# an env_file populates the Settings object and never writes `os.environ`, so
# the variables read straight from the environment — DAILY_CAP_USD,
# PER_RUN_CAP_USD, OPS_DATABASE_URL, the *_WRITER_PASSWORD trio, every
# KEYSTONE_* override — and the VITE_* values Vite inlines are only reachable
# this way.
#
# Precedence: **your shell wins over the file.** A variable already exported is
# left alone, so `DATABASE_URL=postgresql://…/scratch make migrate` overrides
# `.env` exactly as it reads. That matches pydantic-settings and Vite, which
# both rank the real environment above their own files.
#
# Format: `KEY=value`, one per line. Blank lines and `#` comments are skipped;
# anything else is ignored rather than executed — this is a parser, not `source`,
# so a `.env` cannot run commands. Values are taken verbatim: no quote
# stripping, no `$VAR` expansion, no inline comments.
# ---------------------------------------------------------------------------

COMPOSE := docker compose -f infra/docker-compose.yml
UV      := uv --directory service
PNPM    := pnpm --dir dashboard

#: The env file loaded by $(DOTENV). Override to point at another one.
DOTENV_FILE ?= .env

#: Where `make sync` and `make reconcile` post, and under which run id.
API_URL ?= http://localhost:8000
RUN_ID  ?= grader-001

#: `POST /internal/sync` ingests three generations, materializes the canonical
#: layer and then runs the committed invariant rule set over it. Last timed end
#: to end at 59.6s on the full profile (ingest 21.9s + materialize 23.1s +
#: invariants 14.5s, 360,400 records). Do NOT read that split against the
#: scorecard's: `bench:detect-persist-reconcile` clocks invariants at 12.72s,
#: but that is a different rig -- in-process, no HTTP, and no ingest or
#: materialize run ahead of it -- not a newer measurement of this one.
#: Materialize is I/O-bound at COMMIT, so a busy volume stretches it into
#: minutes. The ceiling stays generous for that reason.
SYNC_TIMEOUT ?= 3600

#: `POST /internal/reconcile` scores the conflicts `sync` detected and writes
#: proposals. The isolated reconcile clock in `bench:detect-persist-reconcile` is
#: 8.81s on the graded dataset (3,050 conflicts -> 3,050 proposals) and the
#: end-to-end HTTP call measured 11.2s the last time it was timed by hand, so
#: this ceiling is slack, not an expectation.
RECONCILE_TIMEOUT ?= 900

DOTENV = if [ -f "$(DOTENV_FILE)" ]; then \
	  while IFS= read -r _line || [ -n "$$_line" ]; do \
	    case "$$_line" in ''|'\#'*) continue ;; *=*) ;; *) continue ;; esac; \
	    _key=$${_line%%=*}; \
	    case "$$_key" in ''|*[!A-Za-z0-9_]*) continue ;; esac; \
	    eval "_already_set=\$${$$_key+y}"; \
	    [ -n "$$_already_set" ] || export "$$_line"; \
	  done < "$(DOTENV_FILE)"; \
	fi;

.DEFAULT_GOAL := help
.PHONY: help env up down db-shell migrate db-ready seed seed-dev serve sync reconcile incidents dash suite test lint fmt

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Show the configuration the service will ACTUALLY load (secrets redacted)
	@$(DOTENV) $(UV) run python -c "$$ENV_REPORT_PY"

## --- infrastructure -------------------------------------------------------

up: ## Start Postgres 16 + pgvector (host port 55432) and wait for healthy
	$(COMPOSE) up -d --wait

down: ## Stop the stack (keeps the data volume)
	$(COMPOSE) down

db-shell: ## Open psql inside the running Postgres container
	$(COMPOSE) exec postgres psql -U keystone -d keystone

migrate: ## Apply every migration: schema, the three writer roles, demo API keys, budget scopes
	@$(DOTENV) \
	if [ -z "$${DATABASE_URL:-}" ]; then \
	  echo "make migrate: DATABASE_URL is not set." >&2; \
	  echo "  Run 'cp .env.example .env' first (it ships the local DSN), or export DATABASE_URL." >&2; \
	  exit 1; \
	fi; \
	echo "alembic upgrade head  (cwd: service/)"; \
	$(UV) run alembic upgrade head

db-ready: ## Fail loudly unless DATABASE_URL names a database that is migrated to head
	@$(DOTENV) \
	if [ -z "$${DATABASE_URL:-}" ]; then \
	  echo "DATABASE_URL is not set. Run 'cp .env.example .env' (it ships the local DSN)." >&2; \
	  exit 1; \
	fi; \
	_out=$$($(UV) run alembic current 2>&1) || { \
	  printf '%s\n' "$$_out" >&2; \
	  echo "" >&2; \
	  echo "The database named by DATABASE_URL could not be reached. Run 'make up' first." >&2; \
	  exit 1; \
	}; \
	case "$$_out" in \
	  *"(head)"*) ;; \
	  *) printf '%s\n' "$$_out" >&2; \
	     echo "" >&2; \
	     echo "That database is not migrated to head. Run 'make migrate' first --" >&2; \
	     echo "without it there are no tables, no writer roles and no demo API keys." >&2; \
	     exit 1 ;; \
	esac

## --- application ----------------------------------------------------------

seed: ## Generate the graded dataset + committed golden/ exports (full profile)
	@$(DOTENV) $(UV) run python -m recon.seed --profile full $${SEED:+--seed $$SEED}

seed-dev: ## Inner-loop dataset (~6k records) into .scratch/ -- never the committed tree
	@$(DOTENV) $(UV) run python -m recon.seed --profile dev --out ../.scratch/seed-dev $${SEED:+--seed $$SEED}

serve: db-ready ## Run the FastAPI service on :8000 with reload
	@$(DOTENV) $(UV) run uvicorn recon.app:create_app --factory --reload --port 8000

sync: db-ready ## Load + DETECT: POST /internal/sync (ingest, materialize, invariants; ~1 min+)
	@$(DOTENV) \
	if [ -z "$${TRIGGER_SECRET_SYNC:-}" ]; then \
	  echo "make sync: TRIGGER_SECRET_SYNC is not set, and the trigger fails closed (401)." >&2; \
	  echo "  Run 'cp .env.example .env' -- the same file configures 'make serve', so the" >&2; \
	  echo "  secret this sends is the secret the running service expects." >&2; \
	  exit 1; \
	fi; \
	echo "POST $(API_URL)/internal/sync  run_id=$(RUN_ID)"; \
	echo "  ingesting three generations, materializing entities/links/field_lineage,"; \
	echo "  then running the committed invariant rule set over it (conflicts)."; \
	echo "  On the full profile that is ~1 minute warm, longer when the volume is"; \
	echo "  busy -- the response body prints the per-stage clock. Leave it running."; \
	echo "  This stops at DETECTION -- run 'make reconcile' next for proposals."; \
	_body=$$(mktemp); \
	_code=$$(curl -sS --max-time $(SYNC_TIMEOUT) -o "$$_body" -w '%{http_code}' \
	  -X POST "$(API_URL)/internal/sync" \
	  -H "X-Trigger-Secret: $$TRIGGER_SECRET_SYNC" \
	  -H 'content-type: application/json' \
	  -d '{"run_id":"$(RUN_ID)"}') || { \
	    rm -f "$$_body"; \
	    echo "" >&2; \
	    echo "Could not reach $(API_URL). Start the service with 'make serve' in another terminal." >&2; \
	    exit 1; \
	  }; \
	_out=$$(cat "$$_body"); rm -f "$$_body"; \
	printf '%s\n' "$$_out"; \
	if [ "$$_code" != "200" ]; then \
	  echo "" >&2; \
	  echo "HTTP $$_code from $(API_URL)/internal/sync." >&2; \
	  if [ "$$_code" = "401" ]; then \
	    echo "  401 means TRIGGER_SECRET_SYNC here does not match the one the running" >&2; \
	    echo "  service loaded -- both come from $(DOTENV_FILE)." >&2; \
	  fi; \
	  exit 1; \
	fi; \
	case "$$_out" in \
	  *'"status":"failed"'*|*'"status": "failed"'*) \
	    echo "" >&2; \
	    echo "The sync was accepted but its handler failed; the body above says why." >&2; \
	    exit 1 ;; \
	esac

reconcile: db-ready ## Propose: POST /internal/reconcile (conflicts -> held proposals; ~11 s hand-timed)
	@$(DOTENV) \
	if [ -z "$${TRIGGER_SECRET_RECONCILE:-}" ]; then \
	  echo "make reconcile: TRIGGER_SECRET_RECONCILE is not set, and the trigger fails closed (401)." >&2; \
	  echo "  It is a SEPARATE secret from TRIGGER_SECRET_SYNC on purpose (R19: per-job)." >&2; \
	  echo "  Run 'cp .env.example .env' -- the same file configures 'make serve', so the" >&2; \
	  echo "  secret this sends is the secret the running service expects." >&2; \
	  exit 1; \
	fi; \
	echo "POST $(API_URL)/internal/reconcile  run_id=$(RUN_ID)"; \
	echo "  scoring every conflict 'make sync' detected and writing proposals."; \
	echo "  Every proposal lands held (pending / sensitive_hold); nothing is applied here."; \
	_body=$$(mktemp); \
	_code=$$(curl -sS --max-time $(RECONCILE_TIMEOUT) -o "$$_body" -w '%{http_code}' \
	  -X POST "$(API_URL)/internal/reconcile" \
	  -H "X-Trigger-Secret: $$TRIGGER_SECRET_RECONCILE" \
	  -H 'content-type: application/json' \
	  -d '{"run_id":"$(RUN_ID)"}') || { \
	    rm -f "$$_body"; \
	    echo "" >&2; \
	    echo "Could not reach $(API_URL). Start the service with 'make serve' in another terminal." >&2; \
	    exit 1; \
	  }; \
	_out=$$(cat "$$_body"); rm -f "$$_body"; \
	printf '%s\n' "$$_out"; \
	if [ "$$_code" != "200" ]; then \
	  echo "" >&2; \
	  echo "HTTP $$_code from $(API_URL)/internal/reconcile." >&2; \
	  if [ "$$_code" = "401" ]; then \
	    echo "  401 means TRIGGER_SECRET_RECONCILE here does not match the one the running" >&2; \
	    echo "  service loaded -- both come from $(DOTENV_FILE)." >&2; \
	  fi; \
	  exit 1; \
	fi; \
	case "$$_out" in \
	  *'"status":"failed"'*|*'"status": "failed"'*) \
	    echo "" >&2; \
	    echo "The reconcile was accepted but its handler failed; the body above says why." >&2; \
	    exit 1 ;; \
	esac; \
	case "$$_out" in \
	  *'"proposed":0'*|*'"proposed": 0'*) \
	    echo "" >&2; \
	    echo "The run completed and proposed NOTHING. Two innocent readings and one bad one:" >&2; \
	    echo "  * 'skipped_fingerprint' equal to 'conflicts_seen' -- every conflict already has" >&2; \
	    echo "    an open proposal, so this is R16 de-duplication working (re-running is a no-op);" >&2; \
	    echo "  * 'conflicts_seen':0 -- nothing was detected, so 'make sync' has not run yet;" >&2; \
	    echo "  * anything else is a real defect. The body above distinguishes them." >&2 ;; \
	esac

#: `python -m recon.incidents` -- R25's clustering pass, the writer behind
#: `GET /api/incidents`. Not part of the sync -> reconcile -> suite path: `make
#: suite` regenerates the incidents it truncates on its own (pipeline step 9),
#: so this target is for clustering WITHOUT a graded pass, after a reconcile.
#:
#: It runs the CLI's default budget mode, which charges this run's own
#: ops-provisioned ledger row rather than the mandated daily one. That matters
#: here: one pass costs 56,487 microusd, and the daily row is the deployment's
#: real R17 budget for the day -- local clustering has no business spending it.
#: The older reason written here, "nothing rolls `daily`", is no longer true and
#: is not why: the mandated cap is date-keyed now (`daily:<YYYY-MM-DD>`,
#: `recon.budget.daily_scope_for`) and today's row opens itself the first time a
#: reservation finds it missing (`_open_todays_daily_scope`), so a day's spend
#: does not become a permanent one. A deployment cron with a managed daily budget
#: adds `--charge-daily-cap`; `make incidents ARGS=--charge-daily-cap` does that.
INCIDENTS_ARGS ?=

incidents: db-ready ## Cluster conflicts into incidents (R25) -- what GET /api/incidents serves
	@$(DOTENV) \
	echo "python -m recon.incidents $(INCIDENTS_ARGS)"; \
	echo "  clustering every conflict in DATABASE_URL; one JSON line on stdout."; \
	echo "  Metered on the real ledger. 'daily_cap_scope' in that line names the"; \
	echo "  row this pass charged -- its own by default, never the shared 'daily'."; \
	$(UV) run python -m recon.incidents $(INCIDENTS_ARGS)

dash: ## Run the dashboard dev server
	@$(DOTENV) $(PNPM) dev

suite: db-ready ## Run the committed grading harness and print the scorecard (~35 min)
	@$(DOTENV) \
	if [ -z "$${KEYSTONE_COVERAGE_DATABASE_URL:-}" ]; then \
	  echo "note: KEYSTONE_COVERAGE_DATABASE_URL is unset, so the 'coverage' row's pytest" >&2; \
	  echo "      child runs against the SAME database the other rows are grading. A test" >&2; \
	  echo "      that truncates or re-seeds under it makes those rows describe a database" >&2; \
	  echo "      that no longer exists. See .env.example for the second-database recipe." >&2; \
	  echo "" >&2; \
	fi; \
	$(UV) run python -m recon.suite

## --- quality --------------------------------------------------------------

test: db-ready ## Run service pytest + dashboard vitest
	@$(DOTENV) KEYSTONE_REQUIRE_DB=$${KEYSTONE_REQUIRE_DB:-1} $(UV) run pytest
	$(PNPM) test

lint: ## Lint both packages (no writes)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) lint

fmt: ## Auto-format both packages
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
	$(PNPM) lint --fix

# The `make env` report. Kept out of the recipe so the quoting stays readable;
# `export` puts it in the recipe's environment, where the shell hands it to
# `python -c`. No `#` comments and no `$` in here: make would eat both.
define ENV_REPORT_PY
import os, re
from recon.config import Settings

files = Settings.model_config.get("env_file") or ()
print("env files, lowest precedence first (a real environment variable beats all of them):")
for path in files:
    print("   ", "found " if os.path.isfile(path) else "absent", path)
if not any(os.path.isfile(p) for p in files):
    print()
    print("    None of them exist. Run: cp .env.example .env")

def redact(dsn):
    if not dsn:
        return "(unset)"
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)

s = Settings()
print()
print("as recon.config.Settings resolves it:")
print("  DATABASE_URL             =", redact(s.database_url))
print("  LOG_MODE                 =", s.log_mode)
print("  LLM_PROVIDER             =", s.llm_provider)
print("  LLM_MODEL                =", s.llm_model)
print("  SEED                     =", s.seed)
print("  TRIGGER_SECRET_SYNC      =", "set" if s.trigger_secret_sync else "UNSET (POST /internal/sync will 401)")
print("  TRIGGER_SECRET_RECONCILE =", "set" if s.trigger_secret_reconcile else "UNSET (POST /internal/reconcile will 401)")
print("  ANTHROPIC_API_KEY        =", "set" if s.anthropic_api_key else "unset (fine while LLM_PROVIDER=mock)")
print()
print("read straight from os.environ, so only make's export reaches them:")
for name in (
    "DAILY_CAP_USD",
    "PER_RUN_CAP_USD",
    "OPS_DATABASE_URL",
    "DEMO_ADMIN_API_KEY",
    "KEYSTONE_REQUIRE_DB",
    "KEYSTONE_COVERAGE_DATABASE_URL",
    "VITE_API_BASE_URL",
    "VITE_API_KEY",
):
    value = os.environ.get(name)
    if name.endswith("DATABASE_URL"):
        value = redact(value)
    print("  %-30s = %s" % (name, value if value is not None else "(unset)"))
endef
export ENV_REPORT_PY
