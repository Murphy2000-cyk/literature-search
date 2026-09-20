# Test Case: JSON Output

## Input
```
搜索 "large language model" 的论文，2020-2026年
```

## Expected
1. Save both .txt and .json files
2. JSON contains structured data with all fields

## Verification
- [ ] JSON file created alongside txt file
- [ ] JSON is valid (parseable)
- [ ] JSON contains paper_id, doi, abstract, relevance_score fields
- [ ] JSON count matches actual paper count
