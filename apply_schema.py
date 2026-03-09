"""Apply the SQL schema to Supabase using the REST API."""
import os
import sys
from pathlib import Path
from supabase import create_client
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

def apply_schema():
    """Read and execute the schema SQL via Supabase's rpc or direct REST."""
    sql_path = Path(__file__).parent.parent / "sql" / "001_schema.sql"
    sql = sql_path.read_text()

    # Split into individual statements
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]

    supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

    # We can't run raw SQL via anon key — print instructions instead
    print("=" * 60)
    print("SUPABASE SCHEMA SETUP")
    print("=" * 60)
    print()
    print("The anon key cannot run DDL statements.")
    print("Please run the schema SQL in your Supabase SQL Editor:")
    print()
    print(f"  1. Go to: {SUPABASE_URL.replace('.co', '.co').replace('https://', 'https://supabase.com/dashboard/project/').split('.')[0].replace('https://supabase', 'https://supabase')}")
    project_ref = SUPABASE_URL.replace("https://", "").split(".")[0]
    print(f"  1. Go to: https://supabase.com/dashboard/project/{project_ref}/sql/new")
    print(f"  2. Paste contents of: {sql_path}")
    print(f"  3. Click 'Run'")
    print()
    print("After running the SQL, run: python scripts/seed_data.py")
    print()

    # Test connection
    try:
        result = supabase.table("wallets").select("*").limit(1).execute()
        print("✓ Connection test passed — 'wallets' table exists!")
        print(f"  Found {len(result.data)} existing wallets")
        return True
    except Exception as e:
        if "relation" in str(e) and "does not exist" in str(e):
            print("✗ 'wallets' table not found — please run the schema SQL first")
        else:
            print(f"✗ Connection test: {e}")
        return False

if __name__ == "__main__":
    apply_schema()
