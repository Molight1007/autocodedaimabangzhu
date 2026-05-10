from typing import List, Dict, Any

def distribute_scores(total_score: int, cases: List[Dict[str, Any]]) -> List[int]:
    """
    Distributes the total score among the test cases.

    Rules:
    1. If the source data (vars) contains a "score" key, use it as a weight for distribution.
    2. If no "score" key is found, distribute the total score evenly.
    3. Ensure the sum of distributed scores equals total_score exactly.
    """
    if not cases:
        return []

    # Check if we should use weighted distribution
    # We assume if any case has 'score' in its vars, we use weighted mode for all,
    # treating missing scores as 0.
    use_weights = False
    source_scores = []

    for case in cases:
        vars_data = case.get("vars", {})
        # Check for 'score' or '得分'
        s = vars_data.get("score")
        if s is None:
            s = vars_data.get("得分")
        
        if s is not None:
            use_weights = True
            try:
                val = float(s)
                # Treat negative scores as 0 to avoid negative distribution
                source_scores.append(max(0.0, val))
            except (ValueError, TypeError):
                source_scores.append(0.0)
        else:
            source_scores.append(0.0)

    # If no valid scores found effectively, fall back to even distribution
    if not use_weights or sum(source_scores) == 0:
        count = len(cases)
        base_score = total_score // count
        remainder = total_score % count
        
        # Distribute remainder to the first few cases
        results = []
        for i in range(count):
            score = base_score + (1 if i < remainder else 0)
            results.append(score)
        return results

    # Weighted distribution
    total_weight = sum(source_scores)
    results = []
    current_sum = 0
    
    # First pass: integer distribution based on weight
    # We use the largest remainder method or similar to minimize rounding errors
    # But simple proportion is requested: "按得分的权重比值分配这n分"
    
    # Let's calculate raw shares
    raw_shares = [(s / total_weight) * total_score for s in source_scores]
    
    # Integer parts
    int_shares = [int(s) for s in raw_shares]
    current_sum = sum(int_shares)
    
    # Distribute the remaining points
    remainder = total_score - current_sum
    
    # To distribute remainder fairly, we look at the fractional parts
    # fractional_parts = [raw - int_part for raw, int_part in zip(raw_shares, int_shares)]
    # However, to be simple and robust, we can just add 1 to the first 'remainder' items 
    # that have the highest fractional parts.
    
    diffs = []
    for i, raw in enumerate(raw_shares):
        diffs.append((raw - int_shares[i], i))
    
    # Sort by fractional part descending
    diffs.sort(key=lambda x: x[0], reverse=True)
    
    # Add 1 to the top 'remainder' candidates
    final_scores = list(int_shares)
    for i in range(remainder):
        idx = diffs[i][1]
        final_scores[idx] += 1
        
    return final_scores
