> **TL;DR**: I’ve developed a script to help monitor customer impact (Transaction/Page Load tests) during this Chronium Upgrade. It provides a local dashboard to quickly identify failing tests concentrating all the account groups in a single dashboard.

---

Hi Team,

If you have the same problem as I do: My customers have A LOT of account groups, you know how difficult is to keep an eye on all the organization level tests.

To help us monitor customer impact during the this upgrade activity, I’ve created a tool to track Transaction and Page Load test results.


**Repository (cec_cisco)**: [https://github.com/jduenasp_cisco/te_tam_org_level_test_results](https://github.com/jduenasp_cisco/te_tam_org_level_test_results)

**Installation**:
```bash
git clone https://github.com/jduenasp_cisco/te_tam_org_level_test_results

cd te_tam_org_level_test_results

python -m venv .venv

source ./.venv/bin/activate
pip install -r requirements.txt

python ./app.py
```

Once running, open your browser to: http://127.0.0.1:5050/

### How to use it
1. Select the organization(s) that you want to check, using your API token (or tokens).
2. The script will get all the test results for all the account groups in the selected organizations, excluding savedEvents, disabled tests and LiveShares.
  - The script will save the test results in a local folder,
3. After the script gets the metrics, the dashboard will populate.
4. Check any outstanding test(s).
5. If you noticed tests with issues, even before May 11th, you can ignore them so that you can focus on the tests that have been working successfully only.
6. Switch between your organizations periodically to see if there are any tests with issues.

### Detection Logic for Outstanding Tests
1. **Transaction tests**: When a tx test fails, the test result has this property: `erroType`. When the tx test completes successfully the test result doesn't have this property.
2. **Page Load tests**: When a Page Load test fails, it doesn't have this propoerty: `pageLoadTime`. When the PL test completes successfully, it has this property.

**Current known bugs**:
- Time ranges not working at this moment: Last 2 days and Last 7 days. Please avoid these options.
- In the time line widgets: The latest round sometimes shows "0". However, the outstanding tests are still showing up in their respective tables.
- The "Time With Error" value is not accurate.

Any comments, help and contributions are welcome