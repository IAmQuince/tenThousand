TenThousand Strategy Lab — User Guide
Location: D:\tenThousand\dist\TenThousandStrategyLab

HOW TO RUN THE PROGRAM

Insert the USB drive into your computer

Open File Explorer

Navigate to:

D:\tenThousand\dist\TenThousandStrategyLab

Double-click:

TenThousandStrategyLab.exe

The program will launch in a desktop window.

IMPORTANT:

Do not move the executable out of this folder

Do not delete or modify any files in this folder

The application depends on all included files to run properly

If the program does not launch:

Run "VC_redist.x64.exe" (if provided on the USB)

Then try launching the application again

OVERVIEW OF THE INTERFACE

The application is divided into three main areas:

LEFT PANEL — Strategy Configuration
RIGHT PANEL — Graphs and Results
BOTTOM/STATUS — Logs and simulation feedback

The workflow is:

Configure Strategy → Run Simulation → Analyze Results

CONFIGURING THE STRATEGY

The left panel contains all configurable inputs.

A) Ruleset

Defines how the game is played

Includes scoring behavior, entry conditions, and thresholds

Default values reflect the standard family rule set

Modify only if intentionally testing alternate rules

B) Banking Policy

Controls when the player stops rolling and banks points

Example: bank when turn score exceeds 300

Can include special conditions:

Fewer dice remaining

Near the end of the game (approaching 10,000)

C) Strategy Ladder (Advanced)

Determines how the program chooses which dice to keep each roll

Organized as a priority list (top = highest priority)

Each row represents a rule or preference

You can:

Add rules

Remove rules

Reorder rules (priority matters)

Examples of strategy behavior:

Prefer higher scoring combinations

Prefer keeping more dice to continue rolling

Avoid leaving only one die

D) Presets

Save your current configuration to a file

Load previously saved strategies

Useful for comparing different approaches

RUNNING A SIMULATION

A) Set Simulation Parameters

Number of games (N): typically 1000 or higher

Optional: set a seed for repeatable results

B) Start Simulation

Click "Run Simulation"

A progress indicator will show status

C) During Execution

The program simulates full games automatically

Each game continues until 10,000 points is reached

D) Completion

Results automatically populate in the graphs and summary panels

ANALYZING RESULTS

Use the tabs in the main panel to explore performance:

A) Progress

Shows average score progression vs number of turns

Helps evaluate how quickly strategies accumulate points

B) Turns-to-Win

Distribution of how many turns it takes to reach 10,000

Includes histogram and cumulative distribution

C) Hot Dice

Frequency of rolls where all dice score

Indicates how often high-risk/high-reward situations occur

D) Farkles

Frequency of no-score rolls (turn-ending failures)

Measures risk exposure

E) Risk

Probability of failure based on number of dice remaining

Requires "Detailed Event Log" enabled before simulation

F) Phase Analysis

Breaks down time spent:

Entering the game (before 750 points)

Normal play

End-game (approaching 10,000)

OPTIONAL FEATURES

A) Detailed Event Log

Enables deeper tracking of every roll and decision

Required for certain advanced plots (e.g., Risk tab)

Increases memory usage and runtime

B) Export Options

Export summary data as CSV

Save graphs as PNG images

TROUBLESHOOTING

Program does not open:

Install Microsoft Visual C++ Redistributable (2015–2022)

Ensure all files remain in the folder

Program opens then closes:

Try copying the folder to Desktop and running locally

Check antivirus software

Risk tab shows no data:

Enable "Detailed Event Log" and rerun simulation

GENERAL NOTES

This is a simulation tool — results are statistical, not exact

Larger numbers of simulations produce more stable results

Strategies can be compared by saving and reloading configurations

SUMMARY

Open:
D:\tenThousand\dist\TenThousandStrategyLab

Run:
TenThousandStrategyLab.exe

Configure strategy

Run simulations

Analyze results

If anything behaves unexpectedly, use the built-in diagnostics tools and capture the output for troubleshooting.