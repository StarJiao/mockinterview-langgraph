"""Explicit interview topology. Recovery belongs to the checkpointer, not a router."""

from langgraph.graph import END, START, StateGraph

from .state import InterviewState, route_decision, route_progress


def build_graph(nodes, checkpointer):
    graph = StateGraph(InterviewState)
    for name in (
        "plan",
        "question",
        "answer",
        "decide",
        "probe",
        "finish_question",
        "submit_score",
        "report",
    ):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route_progress, ["question", "report"])
    graph.add_edge("question", "answer")
    graph.add_edge("answer", "decide")
    graph.add_conditional_edges("decide", route_decision, ["probe", "finish_question"])
    graph.add_edge("probe", "answer")
    graph.add_edge("finish_question", "submit_score")
    graph.add_conditional_edges("submit_score", route_progress, ["question", "report"])
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer, name="mock_interview")
