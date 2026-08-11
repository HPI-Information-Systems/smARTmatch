""""
This module implements a similarity function for dimensions attributes.

The similarity function checks for any overlap between two dimensions intervals.
Returns 1.0 if intervals intersect with a 10% tolerance, 0.0 if they don't.
"""

def similarity_function(lost_width:int, lost_height:int, auc_width:int, auc_height:int):
    
    if lost_width is None or lost_height is None or auc_width is None or auc_height is None:
        return None
    
    within_10_percent = lambda a, b: a and b and abs(a - b) / max(a, b) <= 0.10
    
    width_sim = 1.0 if within_10_percent(lost_width, auc_width) else 0.0
    height_sim = 1.0 if within_10_percent(lost_height, auc_height) else 0.0
    return (width_sim + height_sim) / 2.0