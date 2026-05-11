"""
ADR Detection Pipeline — unified runner for shorthand and narrative clinical notes.

Usage:
    # Shorthand style
    python pipeline.py data.xlsx --style shorthand --note-col note_preprocessed

    # Narrative style (e.g., MIMIC discharge summaries)
    python pipeline.py data.xlsx --style narrative --note-col text

Options:
    --style       shorthand | narrative  (default: shorthand)
    --note-col    Column name containing the clinical note text
    --model       OpenRouter model name  (default: google/gemini-3-flash-preview)
"""

import time
import argparse
import json
import os
import warnings
import logging

import pandas as pd
from tqdm import tqdm
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", message=".*non-text parts.*")
warnings.filterwarnings("ignore", message=".*thought_signature.*")
pd.options.mode.chained_assignment = None

from agents import (
    Context_Agent, Medications_Agent, ADRcandidates_Agent,
    ConfoundersCheck_Agent, ConfoundersValidation_Agent, ADR_Agent,
)
from utils import classify_certainty

load_dotenv()


# ============================================================================
# ADR Detection Helper
# ============================================================================
def run_adr_detection(df, input_confounder_col, output_col, agent,
                      note_col, style):
    """Iterate over the DataFrame, call ADR_Agent, and store results."""
    print(f"\n>>> Detecting Side Effects... "
          f"(Using info: {input_confounder_col or 'None'})")

    results = []

    for i in tqdm(range(len(df)), desc=f"Writing to {output_col}"):
        try:
            note = df[note_col][i]
            adr_candidates_raw = df["ADR_candidates"][i]

            # Gather validation / confounder info
            validation_info = ""
            if input_confounder_col and pd.notna(df[input_confounder_col][i]):
                val_data = df[input_confounder_col][i]
                if isinstance(val_data, (list, dict)):
                    validation_info = json.dumps(val_data, ensure_ascii=False)
                else:
                    validation_info = str(val_data)

            # Skip when no candidates
            if (pd.isna(adr_candidates_raw)
                    or str(adr_candidates_raw).strip() in ("", "[]")):
                results.append(json.dumps({"result": "No Side Effect"},
                                          ensure_ascii=False))
                continue

            # Call ADR Agent
            candidates_str = str(adr_candidates_raw)
            context = df["context_note"][i] if style == "shorthand" else None
            response_obj = agent.prompt(
                candidates_str, validation_info, note, context=context,
            )

            results.append(json.dumps(response_obj, ensure_ascii=False))

        except Exception as e:
            print(f"[{i}] Error: {e}")
            results.append(json.dumps({"result": "Error", "msg": str(e)},
                                      ensure_ascii=False))

    df[output_col] = results
    return df


# ============================================================================
# Main Pipeline
# ============================================================================
def process_pipeline(filename, style, note_col, model):
    base_name, ext = os.path.splitext(filename)
    df = pd.read_excel(filename)
    start_time = time.time()

    # Shared kwargs for agent construction
    agent_kwargs = dict(model=model)

    # Instantiate agents
    drug_agent = Medications_Agent(**agent_kwargs)
    candidate_agent = ADRcandidates_Agent(note_style=style, **agent_kwargs)
    confounder_agent = ConfoundersCheck_Agent(note_style=style, **agent_kwargs)
    validation_agent = ConfoundersValidation_Agent(note_style=style, **agent_kwargs)
    adr_agent = ADR_Agent(note_style=style, **agent_kwargs)

    context_agent = None
    if style == "shorthand":
        context_agent = Context_Agent(**agent_kwargs)

    # Initialize columns
    init_cols = ["medications", "ADR_candidates", "confounders"]
    if style == "shorthand":
        init_cols.insert(0, "context_note")
    for col in init_cols:
        if col not in df.columns:
            df[col] = ""

    # ==================================================================
    # STEP 1: Initial Extraction & De-Novo Detection
    # ==================================================================
    print("\n" + "=" * 70)
    print("[STEP 1] Initial Extraction & Detection")
    print("=" * 70)

    for i in tqdm(range(len(df)), desc="Step 1 Extraction"):
        note = df[note_col][i]
        if style == "shorthand":
            df.at[i, "context_note"] = context_agent.prompt(note)
        df.at[i, "medications"] = drug_agent.prompt(note)
        context = df["context_note"][i] if style == "shorthand" else None
        df.at[i, "ADR_candidates"] = candidate_agent.prompt(
            df["medications"][i], note, context=context,
        )

    df = run_adr_detection(
        df, None, "adr_candidates_side_effect", adr_agent, note_col, style,
    )
    df["adr_candidates_side_effect_binary"] = (
        df["adr_candidates_side_effect"].apply(classify_certainty)
    )
    print("Step 1 complete.")

    # ==================================================================
    # STEP 2: Confounder Check & Detection
    # ==================================================================
    print("\n" + "=" * 70)
    print("[STEP 2] Confounder Check & Detection")
    print("=" * 70)

    df["confounders"] = None
    df["confounders"] = df["confounders"].astype(object)

    for i in tqdm(range(len(df)), desc="Step 2 Extraction"):
        note = df[note_col][i]
        context = df["context_note"][i] if style == "shorthand" else None
        df.at[i, "confounders"] = confounder_agent.prompt(
            df["ADR_candidates"][i], note, context=context,
        )

    df = run_adr_detection(
        df, "confounders", "confounder_side_effect", adr_agent,
        note_col, style,
    )
    df["confounder_side_effect_binary"] = (
        df["confounder_side_effect"].apply(classify_certainty)
    )
    print("Step 2 complete.")

    # ==================================================================
    # STEP 3: Validation & Final Detection
    # ==================================================================
    print("\n" + "=" * 70)
    print("[STEP 3] Validation Check & Final Detection")
    print("=" * 70)

    df["confounder_validation"] = None
    df["confounder_validation"] = df["confounder_validation"].astype(object)

    for i in tqdm(range(len(df)), desc="Step 3 Extraction"):
        note = df[note_col][i]
        context = df["context_note"][i] if style == "shorthand" else None
        df.at[i, "confounder_validation"] = validation_agent.ver1_prompt(
            note, df["confounders"][i], context=context,
        )

    df = run_adr_detection(
        df, "confounder_validation", "validation_side_effect", adr_agent,
        note_col, style,
    )
    df["validation_side_effect_binary"] = (
        df["validation_side_effect"].apply(classify_certainty)
    )

    # Save final results — single file with all step outputs
    output_file = f"{base_name}_results{ext}"
    df.to_excel(output_file, index=False)
    print(f"Saved final results: {output_file}")

    elapsed = (time.time() - start_time) / 60
    print(f"\nCompleted. Total time: {elapsed:.1f} min")


# ============================================================================
# CLI
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Multi-Agent ADR Detection Pipeline",
    )
    parser.add_argument("filename", type=str, help="Input Excel file path")
    parser.add_argument(
        "--style", type=str, default="shorthand",
        choices=["shorthand", "narrative"],
        help="Clinical note style (default: shorthand)",
    )
    parser.add_argument(
        "--note-col", type=str, default=None,
        help="Column name for clinical notes "
             "(default: 'note_preprocessed' for shorthand, 'text' for narrative)",
    )
    parser.add_argument(
        "--model", type=str, default="google/gemini-3-flash-preview",
        help="OpenRouter model name (default: google/gemini-3-flash-preview)",
    )

    args = parser.parse_args()

    # Default note column by style
    note_col = args.note_col
    if note_col is None:
        note_col = "note_preprocessed" if args.style == "shorthand" else "text"

    process_pipeline(
        filename=args.filename,
        style=args.style,
        note_col=note_col,
        model=args.model,
    )


if __name__ == "__main__":
    main()
