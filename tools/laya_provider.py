# laya_providor.py

import asyncio
from typing import Any, Dict, Optional, Tuple, Union
from laya import Router


class LayaProvider:
    """Generic wrapper for Laya classification & routing operations.
    
    Provides thread-safe async evaluation with built-in concurrency limiting.
    """

    def __init__(self, max_concurrency: int = 10):
        """Initialize Laya Router and concurrency semaphore.
        
        Args:
            max_concurrency: Max parallel classifications allowed simultaneously.
        """
        self.router = Router()
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def evaluate_binary_choice(
        self,
        context: Union[str, Dict[str, Any]],
        question: str,
        choice_key: str = "is_usable",
        instructions: str = "Is this metadata or asset suitable/usable for answering the given question?",
        yes_criteria: str = "The provided metadata directly contains or supports the necessary information.",
        no_criteria: str = "The provided metadata is irrelevant or insufficient for the question."
    ) -> Tuple[str, Optional[str]]:
        """Generic binary (yes/no) evaluator for table/column metadata against user questions.
        
        Args:
            context: Text or Dict containing metadata (table info, column definitions, etc.).
            question: The user's prompt/query to evaluate against.
            choice_key: Key name for the target decision in the response schema.
            instructions: System guidance for Laya's prediction context.
            yes_criteria: Condition for returning 'yes'.
            no_criteria: Condition for returning 'no'.
            
        Returns:
            Tuple[str, Optional[str]]: ("yes" | "no" | "error", prediction_result)
        """
        # Format input string cleanly whether context is passed as Dict or raw String
        formatted_prompt = (
            f"User Question: {question}\n"
            f"Target Metadata / Context: {context}"
        )

        schema = {
            choice_key: {
                "type": "choice",
                "instructions": instructions,
                "criteria": {
                    "yes": yes_criteria,
                    "no": no_criteria,
                },
            }
        }

        async with self.semaphore:
            try:
                res = await asyncio.to_thread(
                    self.router.predict,
                    formatted_prompt,
                    schema
                )
                
                decision = res.get("answers", {}).get(choice_key, {}).get("choice", "no")
                return decision.lower()
            except Exception as e:
                # Log or handle exception as needed in production
                return f"error: {str(e)}"


# Shared instance: Router() is built once, lazily, and reused everywhere.
_default_provider: Optional[LayaProvider] = None


def get_default_provider() -> LayaProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = LayaProvider()
    return _default_provider

async def evaluate_usability(
    context: Union[str, Dict[str, Any]],
    question: str,
    choice_key: str = "is_usable"
) -> str:
    """Standalone helper function to run quick usability evaluation.
    
    Args:
        context: Table/Column metadata dictionary or string context.
        question: User query string.
        choice_key: Key name for choice classification.
        
    Returns:
        str: "yes", "no", or "error: <details>"
    """
    return await get_default_provider().evaluate_binary_choice(
        context=context,
        question=question,
        choice_key=choice_key
    )