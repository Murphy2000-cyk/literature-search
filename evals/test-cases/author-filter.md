# Test Case: Author Filter

## Input
```
搜索 "deep learning" 的论文，作者 Zhang
```

## Expected
1. Search API fetches papers
2. Client-side filter: author name "Zhang" matches any author in paper.authors
3. Non-matching papers excluded

## Verification
- [ ] All remaining papers have at least one author containing "Zhang"
- [ ] Filter is case-insensitive
