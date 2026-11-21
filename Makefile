.PHONY: up down logs test build clean

# Start the whole stack: backend http://localhost:8000, frontend http://localhost:3000
up:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example - put your real API keys in it.")
	docker compose up --build -d
	@echo ""
	@echo "  frontend  http://localhost:3000"
	@echo "  backend   http://localhost:8000/health"

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

# Backend tests (no network or API keys needed: they run against a local fake provider).
test:
	cd backend && pip install -q -r requirements-dev.txt && python -m pytest -q

# Stop the stack AND delete the SQLite volume.
clean:
	docker compose down -v
