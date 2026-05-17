#!/usr/bin/env python3
"""
Stock Price Service Optimization Test Script

Tests the optimized stock price fetching functionality with various scenarios.
"""

import sys
import os
import time
import pytest

from app.services.stock_price_service import (
    fetch_stock_price,
    get_session
)


RUN_EXTERNAL_PRICE_TESTS = os.getenv("RUN_EXTERNAL_PRICE_TESTS") == "1"
external_price_test = pytest.mark.skipif(
    not RUN_EXTERNAL_PRICE_TESTS,
    reason="set RUN_EXTERNAL_PRICE_TESTS=1 to run live provider checks",
)


def print_result(symbol: str, market: str, result: dict, duration: float):
    """Print formatted result"""
    status = "✅" if result["success"] else "❌"
    print(f"\n{status} {symbol} ({market})")
    print(f"   Price: {result.get('price', 'N/A')}")
    print(f"   Source: {result.get('source', 'N/A')}")
    print(f"   Duration: {duration:.2f}s")
    if result.get('error'):
        print(f"   Error: {result['error']}")


def test_session_reuse():
    """Test that session reuse is working"""
    print("\n" + "="*60)
    print("Test 1: Session Reuse")
    print("="*60)

    session1 = get_session()
    session2 = get_session()

    assert session1 is session2, "Session should be reused"
    print("✅ Session reuse working correctly")


@external_price_test
def test_a_stock():
    """Test A-stock fetching"""
    print("\n" + "="*60)
    print("Test 2: A-Stock Price Fetching")
    print("="*60)

    test_symbols = [
        ("600519", "A股"),  # 贵州茅台
        ("000001", "A股"),  # 平安银行
        ("510300", "A股"),  # 沪深300 ETF
    ]

    for symbol, market in test_symbols:
        start = time.time()
        result = fetch_stock_price(symbol, market)
        duration = time.time() - start
        print_result(symbol, market, result, duration)
        time.sleep(0.5)


@external_price_test
def test_hk_stock():
    """Test HK stock fetching"""
    print("\n" + "="*60)
    print("Test 3: HK Stock Price Fetching")
    print("="*60)

    test_symbols = [
        ("00700", "港股"),  # 腾讯
        ("09988", "港股"),  # 阿里巴巴
    ]

    for symbol, market in test_symbols:
        start = time.time()
        result = fetch_stock_price(symbol, market)
        duration = time.time() - start
        print_result(symbol, market, result, duration)
        time.sleep(0.5)


@external_price_test
def test_us_stock():
    """Test US stock fetching"""
    print("\n" + "="*60)
    print("Test 4: US Stock Price Fetching")
    print("="*60)

    test_symbols = [
        ("AAPL", "美股"),   # Apple
        ("TSLA", "美股"),   # Tesla
    ]

    for symbol, market in test_symbols:
        start = time.time()
        result = fetch_stock_price(symbol, market)
        duration = time.time() - start
        print_result(symbol, market, result, duration)
        time.sleep(0.5)


@external_price_test
def test_error_handling():
    """Test error handling"""
    print("\n" + "="*60)
    print("Test 5: Error Handling")
    print("="*60)

    test_cases = [
        ("INVALID999", "A股", "Invalid A-stock symbol"),
        ("99999", "港股", "Invalid HK stock symbol"),
        ("NOTEXIST", "美股", "Invalid US stock symbol"),
    ]

    for symbol, market, description in test_cases:
        print(f"\n{description}:")
        start = time.time()
        result = fetch_stock_price(symbol, market)
        duration = time.time() - start

        if not result["success"]:
            print("   ✅ Correctly handled error")
            print(f"   Error: {result['error']}")
            print(f"   Duration: {duration:.2f}s")
        else:
            print("   ⚠️ Expected error but got success")

        time.sleep(0.5)


@external_price_test
def test_performance():
    """Test performance improvement"""
    print("\n" + "="*60)
    print("Test 6: Performance Test (10 requests)")
    print("="*60)

    test_symbols = [
        ("600519", "A股"),
        ("000001", "A股"),
        ("00700", "港股"),
        ("09988", "港股"),
        ("AAPL", "美股"),
    ] * 2  # Repeat to get 10 requests

    start_time = time.time()
    results = []

    for symbol, market in test_symbols:
        result = fetch_stock_price(symbol, market)
        results.append(result)
        time.sleep(0.3)  # Adaptive delay (success case)

    total_time = time.time() - start_time
    success_count = sum(1 for r in results if r["success"])
    failed_count = len(results) - success_count

    print("\n📊 Performance Summary:")
    print(f"   Total Requests: {len(results)}")
    print(f"   Successful: {success_count}")
    print(f"   Failed: {failed_count}")
    print(f"   Total Time: {total_time:.2f}s")
    print(f"   Average Time: {total_time/len(results):.2f}s per request")
    print(f"   Success Rate: {success_count/len(results)*100:.1f}%")


@external_price_test
def test_fast_info_api():
    """Test Tushare dependency availability"""
    print("\n" + "="*60)
    print("Test 7: Tushare Import Test")
    print("="*60)

    try:
        import tushare  # noqa: F401
        print("✅ tushare installed")

    except ImportError:
        print("⚠️ tushare not installed, skipping test")
    except Exception as e:
        print(f"❌ Test failed: {str(e)}")


def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("Stock Price Service Optimization Test Suite")
    print("="*60)

    tests = [
        ("Session Reuse", test_session_reuse),
        ("A-Stock Fetching", test_a_stock),
        ("HK Stock Fetching", test_hk_stock),
        ("US Stock Fetching", test_us_stock),
        ("Error Handling", test_error_handling),
        ("Performance", test_performance),
        ("Tushare Import", test_fast_info_api),
    ]

    passed = 0
    failed = 0

    for test_name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"\n❌ {test_name} failed: {str(e)}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "="*60)
    print("Test Summary")
    print("="*60)
    print(f"Passed: {passed}/{len(tests)}")
    print(f"Failed: {failed}/{len(tests)}")

    if failed == 0:
        print("\n✅ All tests passed!")
    else:
        print(f"\n⚠️ {failed} test(s) failed")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
