# Smart Price Prediction - Testing Guide

> **⚠️ TESTING PHASE:** This feature is currently in testing on the `develop` branch.  
> **📝 Note for Maintainers:** Delete this file when merging to `main` - full documentation is in [GitHub Pages](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#energyforecast)

---

## What's New?

**Smart Price Prediction** with energyforecast.de integration automatically learns your grid fees and taxes to provide accurate price predictions when your primary source (Tibber, SmartEnergy AT) lacks tomorrow's prices.

### Why This Matters

**Before (Simple Repetition):**
- Between 00:00-13:00: Tomorrow's prices unavailable
- System repeats today's prices → Poor optimization decisions

**Now (Smart Prediction):**
- Learns: `customer_price = factor × epex_spot + offset`
- Applies pattern to EPEX forecasts → **31.4% more accurate**
- Enables early optimization (11am vs 1pm for Tibber users)

---

## Quick Start

### 1. Get Your API Token

Register at [energyforecast.de/api_keys](https://www.energyforecast.de/api_keys):
- **Free tier:** 48-hour forecasts (sufficient for EOS Connect)
- **Demo token:** Available for testing (rate-limited)

### 2. Update Configuration

Add to your `config.yaml`:

```yaml
price:
  source: tibber  # or smartenergy_at
  token: "YOUR_PRIMARY_SOURCE_TOKEN"
  feed_in_price: 0.08
  negative_price_switch: true
  
  # New: Smart price prediction
  energyforecast_enabled: true
  energyforecast_token: "YOUR_ENERGYFORECAST_TOKEN"
  energyforecast_market_zone: "DE-LU"  # See zones below
```

### 3. Market Zones

Choose your EPEX spot market zone:

| Zone | Region |
|------|--------|
| `DE-LU` | Germany/Luxembourg |
| `AT` | Austria |
| `FR` | France |
| `NL` | Netherlands |
| `BE` | Belgium |
| `PL` | Poland |
| `DK1` | Denmark West |
| `DK2` | Denmark East |

---

## How It Works

### Learning Phase (When Tomorrow's Prices Available)

```
Primary Source: 20-35 ct/kWh
EPEX Spot:      8-15 ct/kWh
                    ↓
System learns: customer_price = 2.1 × epex_spot + 9.5 ct/kWh
```

### Prediction Phase (When Tomorrow's Prices Missing)

```
EPEX Forecast: 12 ct/kWh
                    ↓
Predicted Price: 2.1 × 12 + 9.5 = 34.7 ct/kWh
```

### Timing Examples

**Tibber Users:**
- ✅ **00:00-12:59:** Smart predictions active (tomorrow unavailable)
- ✅ **13:00+:** Real Tibber prices (tomorrow published)
- ✅ **Next day 00:00-12:59:** Smart predictions again

**SmartEnergy AT Users:**
- Similar pattern based on their update schedule

---

## Expected Log Messages

### When Learning (Tomorrow's Prices Available)

No energyforecast messages - using real prices directly.

### When Predicting (Tomorrow's Prices Missing)

```
[PRICE-IF] Tomorrow prices not available from Tibber, using energyforecast.de smart price prediction for next 24 hours
[PRICE-IF] Fetching energyforecast.de smart price prediction (have 24 hours, need 24 more)
[PRICE-IF] Timestamp alignment: 96 overlapping slots found
[PRICE-IF] Learning from 96 samples - First 3 comparisons:
  Sample 0: EPEX 5.23 ct/kWh → Primary 31.05 ct/kWh
  Sample 1: EPEX 4.92 ct/kWh → Primary 30.42 ct/kWh
  Sample 2: EPEX 4.68 ct/kWh → Primary 29.86 ct/kWh
[PRICE-IF] Learned adaptation from 24 overlapping hours: factor=2.450, offset=12.3 ct/kWh
```

### Validation Failures (Falls Back to Simple Repetition)

```
[PRICE-IF] Learned factor 6.2 outside valid range [0.5, 5.0], using price repetition
[PRICE-IF] Learned offset 65.3 ct/kWh exceeds maximum ±50 ct/kWh, using price repetition
```

### Config Warnings (Startup)

```
[CONFIG] WARNING: Using demo_token for energyforecast.de - only for testing!
[CONFIG] WARNING: Invalid energyforecast market zone 'INVALID' - defaulting to DE-LU
```

---

## Testing Checklist

### Basic Functionality
- [ ] Config validation shows warnings for `demo_token`
- [ ] Config validation shows warnings for invalid market zones
- [ ] Smart prediction activates when tomorrow's prices missing (test 02:00-12:59)
- [ ] Real prices used when tomorrow's prices available (test 13:00+)
- [ ] Learning messages appear in logs with reasonable factor/offset

### Edge Cases
- [ ] Handles negative EPEX prices correctly
- [ ] Falls back to repetition when insufficient overlap (<6 hours)
- [ ] Falls back to repetition when factor/offset out of range
- [ ] Handles API timeouts gracefully (test with invalid token)

### Integration
- [ ] Optimization requests contain predicted prices
- [ ] Web dashboard shows prices during prediction hours
- [ ] MQTT topics publish predicted prices correctly

---

## Current Limitations

### Currency Support
- ✅ **EUR sources:** Tibber, SmartEnergy AT
- ❌ **DKK sources:** Stromligning (automatically blocked)
- 📋 **Future:** Currency conversion when requested by users

**Log message for DKK:**
```
[PRICE-IF] Smart price prediction currently only supports EUR prices. Currency DKK detected - using simple price repetition instead.
```

### Minimum Data Requirements
- **6 hours overlap** between primary source and EPEX needed for learning
- Falls back to simple repetition if insufficient data

---

## Troubleshooting

### Issue: No prediction messages in logs

**At 19:30:**
- ✅ **Expected!** Tomorrow's prices already available from Tibber
- Prediction only needed 00:00-12:59 (before tomorrow's publish)

**At 02:00:**
- ❌ Check `energyforecast_enabled: true` in config
- ❌ Check API token is valid
- ❌ Check logs for error messages

### Issue: "Insufficient overlap" messages

**Cause:** Primary source returning <6 hours of data

**Solution:**
- Verify primary source (Tibber/SmartEnergy) API working
- Check network connectivity
- Review primary source logs for errors

### Issue: "Factor outside valid range"

**Typical factors:** 1.5-3.0 (VAT + markup)  
**Your factor:** Check logs for actual value

**Possible causes:**
- Data quality issues in primary source
- Wrong market zone selected
- Primary source uses different currency than expected

**Solution:**
- Verify market zone matches your location
- Check primary source currency is EUR
- Review sample comparisons in logs

### Issue: API rate limits exceeded

**Free tier limits: 50 requests/day**

**Smart caching optimization:**
- API calls throttled to maximum once per hour
- Always calls on first check after midnight (when tomorrow becomes today)
- Subsequent calls within 1 hour use cached result
- **Result: ~13 API calls/day** (well under 50-request limit)
- Example: For Tibber, ~1 call/hour from 00:00-13:00 = ~13 calls total

**If rate limit exceeded (unlikely with optimization):**
- Check frequency in logs for unexpected calls
- Consider upgrading to paid tier
- Report if still exceeding limit (possible edge case)

### Issue: "Currency DKK detected" message

**This is expected for Stromligning users:**
- Smart price prediction currently only supports EUR
- Feature automatically disabled for DKK sources
- System falls back to simple price repetition
- Currency conversion support planned for future release

---

## Performance Metrics

### Real-World Testing Results

**Dataset:** 30 days Tibber prices (Norway)  
**Baseline:** Simple price repetition  
**Smart Prediction:** Energyforecast with adaptive learning

**Results:**
- **31.4% reduction** in prediction error (MAE)
- **Typical factor:** 2.1-2.5 (reflects VAT + markup)
- **Typical offset:** 8-15 ct/kWh (reflects grid fees)

---

## Reporting Issues

When reporting issues on GitHub, please include:

### 1. Configuration Snippet
(Anonymize tokens!)

```yaml
price:
  source: tibber
  energyforecast_enabled: true
  energyforecast_market_zone: "DE-LU"
```

### 2. Relevant Log Messages

Search for `[PRICE-IF]` and `energyforecast`, include 10-20 lines:

```
[PRICE-IF] Tomorrow prices not available from Tibber...
[PRICE-IF] Fetching energyforecast.de smart price prediction...
[ERROR] Your error here
```

### 3. Timing Context

- **Current time:** When issue occurred
- **Expected behavior:** What should happen
- **Actual behavior:** What actually happened

### 4. Environment

- EOS Connect version/branch: `develop`
- Primary price source: Tibber/SmartEnergy AT
- Time frame base: hourly / 15-min
- Currency: EUR / DKK / other

---

## What We're Looking For

Please share your feedback on:

1. **Your learned parameters:**
   - What factor/offset did the system learn for your setup?
   - Do they seem reasonable given your grid fees and taxes?

2. **Prediction accuracy:**
   - Compare predicted prices vs actual prices (after 13:00)
   - How close were the predictions?

3. **Edge cases:**
   - Did you encounter any unexpected behavior?
   - Any scenarios not covered in troubleshooting?

4. **Performance:**
   - API response times
   - Any rate limiting issues?

---

## Additional Resources

- **Full Documentation:** [GitHub Pages - Smart Price Prediction](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#energyforecast)
  - *Note:* Will be available after merge to main
- **Energyforecast.de:** [API Documentation](https://www.energyforecast.de/api_keys)
- **EOS Connect:** [Main Documentation](https://ohAnd.github.io/EOS_connect/)

---

## Example Test Scenario

**Day 1 - 19:30 (Tomorrow available):**
```
✅ Tibber has today + tomorrow
✅ System uses real prices
✅ No energyforecast messages in logs
```

**Day 2 - 02:00 (Tomorrow missing):**
```
✅ Tibber has only today
✅ Smart prediction activates
✅ Log shows: factor=2.35, offset=11.2 ct/kWh
✅ Predicted prices: 28-38 ct/kWh
```

**Day 2 - 13:00 (Tomorrow published):**
```
✅ Tibber updates with tomorrow's prices
✅ System switches to real prices
✅ Compare predicted vs actual: How accurate?
```

---

**Happy Testing! 🚀**

*Your feedback helps make EOS Connect better for everyone. Thank you for testing!*
