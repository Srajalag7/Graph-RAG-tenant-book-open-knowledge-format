# Setup and Evaluation Guide

This service provides the backend API and frontend for the housing assistant. It is built using Python (FastAPI). No GPU or vector database is required. 

## Requirements
- Python 3.10+
- Network access
- API key for the LLM

## Configuration

1. Create and activate a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r service/requirements.txt
```

2. Configure environment variables (create a `.env` file based on `.env.example`):
- `GEMINI_API_KEY`: Your API key.
- `MODEL`: Tested with `gemini-3.5-flash-lite`.
- `BASE_URL`: `https://generativelanguage.googleapis.com/v1beta/openai` (omit `/chat/completions`).

**Important Note on API Compatibility:** This system is Gemini-compatible and uses Gemini 3.5 Flash-Lite (free tier). Direct testing with the standard OpenAI API has not been extensively performed, so some schema strictness issues might arise while running. If you prefer using a direct OpenAI endpoint, please configure the OpenAI model in the environment variables; if any issues occur during the run, I can easily fix the schema to ensure full OpenAI compatibility.

## Running the Evaluator (Held-out questions)

Run the following command to reproduce the held-out submission exactly as requested:
```bash
CORPUS_DIR=corpus PORT=8000 bash scripts/reproduce.sh
```
This script will:
- Install dependencies
- Rebuild the Open Knowledge Format (OKF) bundle
- Start the service on port 8000
- Run the held-out questions
- Produce `results/answers.jsonl` and `results/manifest.json` containing token counts and latency.

## Running the Frontend

To start the API service and the frontend together with a single command:
```bash
CORPUS_DIR=corpus bash scripts/serve.sh --port 8000
```
Then, open **http://127.0.0.1:8000** in your browser.

## Freshness Update Evaluation

To evaluate how the system handles a rule change using the updated corpus:
1. Reindex the new corpus and start the server:
```bash
export CORPUS_DIR=/absolute/path/to/corpus_v2
export PORT=8000
bash scripts/reindex.sh "$CORPUS_DIR"
bash scripts/serve.sh
```

2. In a separate terminal, run the freshness questions:
```bash
source venv/bin/activate
python service/run_questions.py \
  --questions questions/freshness_subset.jsonl \
  --out results/answers_v2.jsonl \
  --url http://127.0.0.1:8000
```
