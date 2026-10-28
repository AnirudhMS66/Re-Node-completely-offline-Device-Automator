"""Re-Node desktop control center (Tkinter, standard library only).

Logic modules (state, log_bus, workers, demo) never import tkinter, so they can be unit-tested
headless. Only widgets.py and app.py touch Tk.
"""
