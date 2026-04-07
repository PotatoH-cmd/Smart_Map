
import sys
import os
import logging

# Add current directory to path so we can import tools
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.postgresql_tool import PostgreSQLTool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_get_db_schema():
    # Use default config from the tool
    tool = PostgreSQLTool()
    
    print("--- Testing get_db_schema with fresh connection ---")
    res = tool._get_db_schema()
    if res['success']:
        print("Success: Got DB schema")
    else:
        print(f"Failed: {res.get('error')}")

    print("\n--- Testing get_db_schema after closing connection (simulating failure) ---")
    if tool.conn:
        tool.conn.close()
        print("Connection manually closed.")
    
    res = tool._get_db_schema()
    if res['success']:
        print("Success: Recovered and got DB schema")
    else:
        print(f"Failed: {res.get('error')}")

if __name__ == "__main__":
    test_get_db_schema()
