# Restaurant Shift Scheduling System

A web-based scheduling tool that combines a constraint-based solver with a natural language chat interface, allowing restaurant managers to generate and adjust weekly staff schedules using plain English commands.

---

# How to Run

## Prerequisites
- Python 3.10 or above
- A Groq API key, available for free at console.groq.com

## Installation

Install the required Python dependencies:

```bash
pip install flask flask-cors ortools requests
```

## Running the System

The system requires two processes running simultaneously.

In the first terminal, start the Flask backend:

```bash
GROQ_API_KEY=your-key python app.py
```

In the second terminal, start the frontend server:

```bash
python -m http.server 3000
```

Then open `http://localhost:3000` in a browser.

## Windows

On Windows, set the environment variable separately before starting the backend:

```bash
set GROQ_API_KEY=your-key
python app.py
```

## Notes
- The backend runs on port 5001 by default
- Clearing browser data will erase all saved schedules and settings, as the system uses localStorage for persistence
- The Groq free tier has rate limits; avoid sending requests in rapid succession
