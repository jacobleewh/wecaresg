"""Live Gemini triage. Demo narratives are inputs only, never recommendations."""
from gemini_triage import TriageError, analyze_hardship

DEMO_SCENARIOS = {
    "A": {
        "label": "Retrenched father of 2, rental flat, arrears mounting",
        "text": ("I got retrenched last month, no income right now. I have 2 kids in "
                  "primary school. We stay in a rental flat and the rental arrears are "
                  "mounting, landlord already warned us. Really worried about eviction."),
    },
    "B": {
        "label": "Elderly widow with chronic illness & mobility limits",
        "text": ("I am a 74 year old widow living alone in a 2-room rental flat. I have "
                  "chronic kidney failure and need dialysis twice a week. I also have "
                  "mobility limits and it's hard to get to the clinic. No income besides a "
                  "small pension."),
    },
    "C": {
        "label": "Single working mother seeking infant care subsidies",
        "text": ("I'm a single mother with a 3 month old newborn baby. I recently went back "
                  "to work but my salary is only $1600 a month and childcare fees are too "
                  "expensive. I need infant care subsidy support urgently."),
    },
}


