# Sh'elah App

Sh'elah is a Flask-based Jewish learning and halachic assistant that combines:
- text browsing from Sefaria
- community customs data
- prayer access
- zmanim and holiday context
- AI-assisted Sh'elah synthesis

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```
If on Mac, use this instead:
```bash
pip install -r requirements.txt --break-system-packages
```
3. Set environment variables (see `.env.example`).
4. Run the app:

```bash
cd ~/(Location_of_file)
python app.py
```
If on Mac, use this instead:
```bash
cd ~/(Location_of_file)
python3 app.py
```
Default local URL: http://127.0.0.1:5001

## Project Structure

- `app.py`: Flask app and API routes
- `templates/`: HTML templates
- `static/`: CSS and static assets
- `customs/`: community customs JSON datasets
- `All md files/`: implementation/audit docs

## Environment Variables

- `FLASK_SECRET_KEY`: required for stable sessions
- `ANTHROPIC_API_KEY`: optional, enables Claude-backed responses
- `PORT`: optional override for server port

## Notes for GitHub

This repository is configured to ignore local caches, virtual environments, and `.env` files.
If you publish publicly, ensure your deployment uses environment variables and does not expose secrets.
