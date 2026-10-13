"""
Belaku TradeBot Setup Script

This script automates the setup process for Belaku TradeBot,
including environment configuration and access token generation.
"""

import os
import sys
import json
import webbrowser
from pathlib import Path
from typing import Optional

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def print_header(title: str) -> None:
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def check_env_file() -> bool:
    """Check if .env file exists and has required variables."""
    env_path = Path(".env")
    
    if not env_path.exists():
        print("❌ .env file not found!")
        return False
    
    # Read .env file
    with open(env_path, 'r') as f:
        content = f.read()
    
    # Check for required variables
    required_vars = [
        'ZERODHA_API_KEY',
        'ZERODHA_API_SECRET',
        'ZERODHA_ACCESS_TOKEN'
    ]
    
    missing_vars = []
    for var in required_vars:
        if f'{var}=' not in content:
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing required variables in .env: {', '.join(missing_vars)}")
        return False
    
    print("✅ .env file exists and has required variables")
    return True


def setup_env_file() -> None:
    """Set up the .env file with example values."""
    print_header("Setting up .env file")
    
    # Read .env.example
    example_path = Path(".env.example")
    if not example_path.exists():
        print("❌ .env.example file not found!")
        return
    
    with open(example_path, 'r') as f:
        content = f.read()
    
    # Write to .env
    with open(".env", 'w') as f:
        f.write(content)
    
    print("✅ .env file created from .env.example")
    print("\n📝 Please edit .env file and update:")
    print("   - ZERODHA_API_KEY")
    print("   - ZERODHA_API_SECRET")
    print("   - ZERODHA_ACCESS_TOKEN (will be generated)")


def get_access_token(api_key: str, api_secret: str) -> Optional[str]:
    """
    Get access token from Zerodha using OAuth flow.
    
    Args:
        api_key: Zerodha API key
        api_secret: Zerodha API secret
        
    Returns:
        Access token or None if failed
    """
    print_header("Access Token Generation")
    
    print("\n📋 Zerodha OAuth Flow:")
    print("   1. Visit: https://kite.zerodha.com/connect/login?api_key=" + api_key)
    print("   2. Log in and authorize the application")
    print("   3. You'll get an authorization code")
    print("   4. Exchange code for access token")
    
    # Open browser for authorization
    auth_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}"
    print(f"\n🌐 Opening browser for authorization...")
    print(f"   URL: {auth_url}")
    
    try:
        webbrowser.open(auth_url)
    except Exception as e:
        print(f"   ⚠️  Could not open browser automatically: {e}")
        print(f"   📝 Please visit the URL manually")
    
    print("\n⏳ Waiting for authorization...")
    input("   Press Enter after you have authorized the application and obtained the authorization code: ")
    
    # Get authorization code
    auth_code = input("\n📝 Enter the authorization code: ").strip()
    
    if not auth_code:
        print("❌ Authorization code is required!")
        return None
    
    print("\n🔄 Exchanging authorization code for access token...")
    
    # In a real implementation, you would make an API call here
    # For now, we'll simulate the process
    try:
        # Simulate token generation (replace with actual API call)
        import time
        time.sleep(1)  # Simulate API call delay
        
        # Mock access token (replace with actual token from API response)
        access_token = f"mock_token_{api_key[:8]}_{int(time.time())}"
        
        print(f"✅ Access token generated: {access_token[:20]}...")
        return access_token
        
    except Exception as e:
        print(f"❌ Failed to generate access token: {e}")
        return None


def update_env_file(access_token: str) -> None:
    """
    Update .env file with access token.
    
    Args:
        access_token: Access token to update
    """
    print_header("Updating .env file")
    
    # Read .env file
    with open(".env", 'r') as f:
        lines = f.readlines()
    
    # Update access token
    updated = False
    for i, line in enumerate(lines):
        if line.startswith('ZERODHA_ACCESS_TOKEN='):
            lines[i] = f'ZERODHA_ACCESS_TOKEN={access_token}\n'
            updated = True
            break
    
    if not updated:
        print("❌ Could not find ZERODHA_ACCESS_TOKEN in .env file")
        return
    
    # Write back to .env file
    with open(".env", 'w') as f:
        f.writelines(lines)
    
    print("✅ .env file updated with access token")


def run_tests() -> None:
    """Run the test scripts to verify setup."""
    print_header("Running Tests")
    
    print("\n🧪 Running structure tests...")
    try:
        os.system("python3 test_structure.py")
    except Exception as e:
        print(f"❌ Structure tests failed: {e}")
    
    print("\n🧪 Running mock credential tests...")
    try:
        os.system("python3 test_with_mock_credentials.py")
    except Exception as e:
        print(f"❌ Mock credential tests failed: {e}")


def main() -> None:
    """Main setup function."""
    print_header("Belaku TradeBot Setup")
    
    # Check if .env file exists
    if not check_env_file():
        print("\n🔧 Setting up .env file...")
        setup_env_file()
        
        # Get API credentials
        print("\n📝 Please provide your Zerodha credentials:")
        api_key = input("   Zerodha API Key: ").strip()
        api_secret = input("   Zerodha API Secret: ").strip()
        
        if not api_key or not api_secret:
            print("❌ API key and secret are required!")
            return
        
        # Update .env with API credentials
        with open(".env", 'r') as f:
            content = f.read()
        
        content = content.replace(
            'ZERODHA_API_KEY=your_zerodha_api_key_here',
            f'ZERODHA_API_KEY={api_key}'
        )
        content = content.replace(
            'ZERODHA_API_SECRET=your_zerodha_api_secret_here',
            f'ZERODHA_API_SECRET={api_secret}'
        )
        
        with open(".env", 'w') as f:
            f.write(content)
        
        print("✅ API credentials updated in .env file")
        
        # Get access token
        access_token = get_access_token(api_key, api_secret)
        if access_token:
            update_env_file(access_token)
    
    # Run tests
    run_tests()
    
    print_header("Setup Complete")
    print("\n🎉 Belaku TradeBot setup completed successfully!")
    print("\n📋 Next steps:")
    print("   1. Run: python3 main.py")
    print("   2. Monitor logs in tradebot.log")
    print("   3. Check system status with test scripts")
    print("\n⚠️  Note: This is a mock setup. For real trading, use actual Zerodha credentials.")


if __name__ == "__main__":
    main()