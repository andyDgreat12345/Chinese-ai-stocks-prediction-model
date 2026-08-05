"""Paper-trading simulator — the system's calls run through human trader logic.

`trader` holds the decision rules (conviction filter, risk-based sizing, stops,
targets, position limits); `engine` replays them day by day over real history and
verifies the result against buy-and-hold.
"""
