import os
from typing import Annotated, TypedDict
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, AnyMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from .tmdb_service import fetch_movie_poster

# 1. Define the Global State (The "Memory" shape)
class AgentState(TypedDict):

    messages: Annotated[list[AnyMessage], add_messages]
    watched_list: list[str]

# 2. Setup Gemma and give it the TMDb Tool
llm = ChatGoogleGenerativeAI(model="gemma-4-31b-it",temperature=0.1)
tools = [fetch_movie_poster]
llm_with_tools = llm.bind_tools(tools)

# 3. The System Prompt (Notice we don't ask for JSON anymore!)
SYS_PROMPT = """You are MovieMate AI, a sophisticated film concierge.
Follow this strict protocol:
1. GATHER: Ensure you know their Genre, Mood, and Streaming Platform. Ask casually if missing.
2. CHECK MEMORY: Never recommend movies listed in the user's watched list.
3. RECOMMEND: Once you have the info, suggest 1-2 movies. 
CRITICAL: You MUST use the 'fetch_movie_poster' tool for EVERY movie you recommend before giving your final reply.

FORMATTING RULES:
- DO NOT include image URLs, links, or markdown image tags (like ![title](url)) in your text reply.
- The frontend app will display the movie posters automatically based on the tool data.
- Keep your text reply clean, conversational, and completely free of raw URLs.
"""

# 4. The AI Node Logic
async def chatbot_node(state: AgentState):
    sys_msg = SystemMessage(content=SYS_PROMPT)
    
    # Inject the watched list into the prompt dynamically
    if state.get("watched_list"):
        sys_msg.content += f"\n\nUSER'S WATCHED LIST (DO NOT RECOMMEND): {', '.join(state['watched_list'])}"
        
    response = await llm_with_tools.ainvoke([sys_msg] + state["messages"])
    return {"messages": [response]}

# 5. Build the LangGraph
workflow = StateGraph(AgentState)

# Add our nodes
workflow.add_node("agent", chatbot_node)
workflow.add_node("tools", ToolNode(tools)) # Built-in LangGraph node for executing tools

# Define the flow (Agent -> Tools -> Agent -> End)
workflow.add_edge(START, "agent")

# Conditional logic: If the agent decides to use a tool, go to "tools", else END
workflow.add_conditional_edges(
    "agent",
    lambda state: "tools" if state["messages"][-1].tool_calls else END
)
workflow.add_edge("tools", "agent")

# 6. Attach the Checkpointer (This handles the session memory automatically!)
memory = MemorySaver()
app = workflow.compile(checkpointer=memory)