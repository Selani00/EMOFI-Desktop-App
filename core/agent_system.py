from typing import Dict, List, Optional, Any
import base64
from collections import Counter
import re
import time
from langgraph.graph import StateGraph, END
import requests
from utils.desktop import capture_desktop
from ui.notification import send_notification, execute_task
import ollama
import ctypes
from collections import Counter
from typing import List, Optional
from pydantic import BaseModel
from core.recommender_tools import open_recommendation
from old_utils.runner_interface import launch_window
from utils.tools import open_recommendations
from database.db import get_apps, get_connection,add_agent_recommendations
from dotenv import load_dotenv
import os
import json
from pydantic import BaseModel, Field

load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY")
QWEN_API_KEY = os.getenv("QWEN_API_KEY")

def run_agent_system(emotions):
    initial_state = AgentState(
        emotions=emotions,
        average_emotion=None,
        detected_task=None,
        recommendation=None,
        recommendation_options= [],
        executed=False,
        action_executed=None,
        action_time_start=0
        
    )
    agent_workflow = create_workflow()
    return agent_workflow.invoke(initial_state)

class AppRecommendation(BaseModel):
    app_name: str = Field(description="Name of recommended application")
    app_url: str = Field(description="URL or local path of the application")
    search_query: str = Field(description="Search query if web-based application")
    is_local: bool = Field(default=False, description="Whether the app is a local executable")

class RecommendationResponse(BaseModel):
    recommendation: str = Field(description="4-word mood improvement suggestions")
    recommendation_options: List[AppRecommendation] = Field(description="Two app recommendations")
class RecommendationList(BaseModel):
    listofRecommendations: List[RecommendationResponse] = Field(description="List of 3 recommendations with options")

class AgentState(BaseModel):
    emotions: List[str]
    average_emotion: Optional[str]
    detected_task: Optional[str]
    recommendation: Optional[List[str]]  # list of suggestions
    recommendation_options: Optional[List[List[AppRecommendation]]]
    executed: Optional[bool]
    action_executed: Optional[str]
    action_time_start: Optional[float]


def create_workflow():
    workflow = StateGraph(AgentState)
    workflow.add_node("calculate_emotion", average_emotion_agent)
    workflow.add_node("detect_task", task_detection_agent)
    workflow.add_node("generate_recommendation", recommendation_agent)
    workflow.add_node("execute_action", task_execution_agent)
    workflow.add_node("exit_action", task_exit_agent)
    workflow.set_entry_point("calculate_emotion")
    workflow.add_edge("calculate_emotion", "detect_task")
    workflow.add_edge("detect_task", "generate_recommendation")
    workflow.add_edge("generate_recommendation", "execute_action")
    workflow.add_edge("execute_action", "exit_action")
    workflow.add_edge("exit_action", END)
    return workflow.compile()

def average_emotion_agent(state):
    """Calculate most frequent emotion from AgentState model"""
    if not state.emotions:
        return {"average_emotion": "neutral"}
    print(f"[Agent] Emotions: {state.emotions}")
    counter = Counter(state.emotions)
    most_common = counter.most_common(1)[0][0]
    print(f"[Agent] Average emotion: {most_common}")
    return {"average_emotion": most_common}

# Remove reasoning tags from the response
def clean_think_tags(text):
    cleaned_text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return cleaned_text.strip()


def task_detection_agent(state):
    try:
        if state.average_emotion == "Neutral" or state.average_emotion == "Happy" or state.average_emotion == "Surprise":
            print("[Agent] No task detection needed for neutral emotion.")
            return {"detected_task": "No Need to Detect Task"}
        # Capture screenshot as a base64 string (possibly with prefix)
        screenshot = capture_desktop()
        if not screenshot:
            raise ValueError("Failed to capture screenshot")
        # Remove data URI prefix if present
        if screenshot.startswith('data:image'):
            screenshot = screenshot.split(',')[1]

        # Validate base64 string (optional, for debugging)
        try:
            base64.b64decode(screenshot)
        except Exception as decode_err:
            raise ValueError(f"Invalid base64 screenshot: {decode_err}")

        # Send the raw base64 string (no prefix) to Ollama
        # response = ollama.generate(
        #     model="llava:7b",
        #     prompt="Describe user's current activity. Focus on software and tasks.",
        #     images=[screenshot]
        # )
        headers = {
            "Connection": "close",  # Disable keep-alive
            "Content-Type": "application/json"
        }
        response = requests.post(
            "https://fa7a43f295fa.ngrok-free.app/api/generate",
            headers=headers,
            json={
                "model": "llava:7b",
                "prompt": "Describe user's current activity. Focus on software and tasks.",
                "images": [screenshot],
                "stream": False
            }
        )

        # Handle HTTP errors
        if response.status_code != 200:
            print(f"API error ({response.status_code}): {response.text[:100]}...")
            return {"detected_task": "unknown"}

        # Parse JSON response
        response_data = response.json()
        detected_task = response_data.get('response', '').strip()
        state.detected_task = detected_task
        print(f"Detected task: {detected_task}")
        return {"detected_task": detected_task}

    except Exception as e:
        print(f"Error detecting task: {str(e)}")
        return {"detected_task": "unknown"}
    


def parse_llm_response(text):
    try:
        # Clean <think> tags if present
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

        # Extract recommendation
        rec_match = re.search(r'recommendation:\s*(.+)', text)
        recommendation = rec_match.group(1).strip() if rec_match else None

        # Extract recommendation_options (raw list inside [])
        options_match = re.search(r'recommendation_options:\s*\[(.*)\]', text, re.DOTALL)
        options_raw = options_match.group(1).strip() if options_match else ""

        # Convert (app_name: 'X', ...) → {"app_name": "X", ...}
        options = []
        for option_text in re.findall(r'\((.*?)\)', options_raw, re.DOTALL):
            entry = {}
            for kv in option_text.split(','):
                key, val = kv.split(':', 1)
                entry[key.strip()] = val.strip().strip("'\"")
            options.append(entry)

        return recommendation, options

    except Exception as e:
        print("[Agent] Error parsing LLM response block:", e)
        return None, []

def extract_json_from_text(text):
    try:
        # Find JSON between ```json and ```
        match = re.search(r'```json\s*(\{.*?\}|\[.*?\])\s*```', text, re.DOTALL)
        if match:
            return match.group(1)

        # If no code block, try to parse whole text
        if text.strip().startswith("{") or text.strip().startswith("["):
            return text.strip()

        raise ValueError("No valid JSON found in text.")
    except Exception as e:
        print("[Agent] JSON extraction failed:", e)
        return None
    
def recommendation_agent(state):
    if not state.detected_task or "No Need to Detect Task" in state.detected_task:
        print("[Agent] No task detected – skipping recommendation.")
        return {"recommendation": ["No action needed"], "recommendation_options": []}

    emotion = state.average_emotion
    task = state.detected_task
    print(f"[Agent] Processing for emotion={emotion!r}, task={task!r}")

    negative_emotions = ["Angry", "Sad", "Fear", "Disgust", "Stress", "Boring"]
    if emotion not in negative_emotions:
        print("[Agent] Mood is fine – no recommendation.")
        return {"recommendation": ["No action needed"], "recommendation_options": []}

    conn = get_connection()
    if not conn:
        print("[Agent] DB connection failed – skip recommendation.")
        return {"recommendation": ["No action needed"], "recommendation_options": []}

    available_apps = get_apps(conn)
    print("[Agent] Available apps:", available_apps)
    prompt = f"""
            User feels {emotion} while working on: {task}.
            Looking for 3 four-word mood-improvement suggestions.

            Installed apps (category|name|path):
            {available_apps!r}

            Return ONLY valid JSON. No text, no notes. Output must be an array of 3 objects:
            - recommendation: exactly four words
            - recommendation_options: array of 2 items each with:
                app_name (str),
                app_url (URL or local path),
                search_query (str, only for web apps),
                is_local (bool)
            No duplicates, prefer web apps. All URLs must start with "https://". Each local app sets `is_local: true`.
            """

    full_schema = RecommendationList.model_json_schema()

    try:
        # res = requests.post(
        #     url="https://openrouter.ai/api/v1/chat/completions",
        #     headers={
        #         "Authorization": f"Bearer {QWEN_API_KEY}",
        #         "Content-Type": "application/json"
        #     },
        #     data=json.dumps({
        #         "model": "deepseek/deepseek-r1-0528-qwen3-8b:free",
        #         "messages": [
        #             {"role": "system", "content": "You are an assistant. Output must be valid JSON only."},
        #             {"role": "user", "content": prompt}
        #         ],
        #         "response_format": {
        #             "type": "json_schema",
        #             "json_schema": {
        #                 "name": "recommendation_list",
        #                 "strict": True,
        #                 "schema": full_schema
        #             }
        #         },
        #         "structured_outputs": True
        #     }
        # ))

        schema = {
            "type": "object",
                    "properties": {
                        "listofRecommendations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "recommendation": {"type": "string"},
                                    "recommendation_options": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "app_name": {"type": "string"},
                                                "app_url": {"type": "string"},
                                                "search_query": {"type": "string"},
                                                "is_local": {"type": "boolean"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
            }

        res = requests.post(
             "https://fa7a43f295fa.ngrok-free.app/api/generate",  # Use local endpoint
            headers={"Content-Type": "application/json"},
            json={
                "model": "qwen3:4b",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
                "format": schema    
            }
        )


        print("[Agent] API response:", res.json())
        if res.status_code != 200:
            print(f"[Agent] API returned status {res.status_code}: {res.text[:200]}")
            return {"recommendation": ["No action needed"], "recommendation_options": []}

        # raw_content = res.json()["choices"][0]["message"]["content"]
        raw_content = res.json()["response"]
        print("Raw Response Content:", raw_content)

        try:
            parsed_data = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        except json.JSONDecodeError:
            print("[Agent] Failed to decode JSON.")
            return {"recommendation": ["No action needed"], "recommendation_options": []}

        if "listofRecommendations" not in parsed_data or not isinstance(parsed_data["listofRecommendations"], list):
            print("[Agent] Parsed data is not a valid list of dicts.")
            return {"recommendation": ["No action needed"], "recommendation_options": []}

        try:
            recommendation_objects = [RecommendationResponse(**item) for item in parsed_data["listofRecommendations"]]
        except Exception as e:
            print("[Agent] Exception parsing recommendation objects:", e)
            return {"recommendation": ["No action needed"], "recommendation_options": []}

        resp_data = RecommendationList(listofRecommendations=recommendation_objects)

        # Extract recommendations
        recommendations_list = [rec.recommendation for rec in resp_data.listofRecommendations]
        recommendation_options_list = [rec.recommendation_options for rec in resp_data.listofRecommendations]

        print("Final Recommendations:", recommendations_list)
        print("Options:", recommendation_options_list)        
        
        # Update state
        state.recommendation = recommendations_list
        state.recommendation_options = recommendation_options_list

        try:
            for i, recommendation_type in enumerate(recommendations_list):
                for option in recommendation_options_list[i]:
                    recommed_app = option.app_name
                    app_url = option.app_url
                    search_query = option.search_query
                    is_local = option.is_local

                    add_agent_recommendations(
                        conn,
                        1,
                        recommendation_type,
                        recommed_app,
                        app_url,
                        search_query,
                        is_local
                    )
        except Exception as e:
            print("[Agent] Error adding recommendations to DB:", e)


        return {
            "recommendation": recommendations_list,
            "recommendation_options": recommendation_options_list
        }

    except Exception as e:
        print("[Agent] Unexpected error:", e)
        return {
            "recommendation": ["No action needed"],
            "recommendation_options": [],
            "listofRecommendations": RecommendationList(listofRecommendations=[])
        }



def send_blocking_message(title, message):
    MB_OK = 0x0
    ctypes.windll.user32.MessageBoxW(0, message, title, MB_OK)

def task_execution_agent(state):
    recommended_output = state.recommendation
    recommended_options = state.recommendation_options
    
    print("List of Recommendations in task_execution_agent: ", recommended_output)
    if "No action needed" not in recommended_output:
        chosen_recommendation = send_notification("Recommendations by EMOFI", recommended_output,recommended_options)
        print("Chosen recommendation: ", chosen_recommendation)
        if chosen_recommendation:
            print("Opening recommendations...")
            open_recommendations(chosen_recommendation)
            return {
                    "executed": True,
                }
                    
def task_exit_agent(state):
    task_executed = True
    if not state.executed:
        return {"executed": False, "action_time_start": None}
    print("Thread is running")
    while task_executed:
        time.sleep(10)
        task_executed = False
    print("Thread is closed")
    return {"executed": False, "action_time_start": None}


# def recommendation_agent(state):
#     # Early exit if no task detected
#     if "No Need to Detect Task" in state.detected_task or not state.detected_task:
#         print("[Agent] No task detected, skipping recommendation.")
#         return {"recommendation": "No action needed"}
    
#     emotion = state.average_emotion.lower()
#     detected_task = state.detected_task.lower() if state.detected_task else "unknown"
#     print(f"[Agent] Calculating recommendation for emotion: {emotion} and task: {detected_task}")
    
#     negative_emotions = ["angry", "sad", "fear", "disgust", "stress", "boring"]
    
#     # Exit if not negative emotion
#     if emotion not in negative_emotions:
#         return {"recommendation": "No action needed"}

#     prompt = f"""
#         User is feeling {emotion} and is currently working on: {detected_task}.
#         Suggest one concrete action to improve mood from this list. Priority order:
#         1. Play music
#         2. Watch funny videos
#         3. Take a break
#         4. Quick game
#         5. Only if user is coding and needs help: "Coding Bot"
#         6. Nothing

#         Respond ONLY with the exact phrase from the list.
#     """
#     try:
#         response = requests.post(
#             "https://087f647be26e.ngrok-free.app/api/generate",  # Use local endpoint
#             headers={"Content-Type": "application/json"},
#             json={
#                 "model": "qwen3:4b",
#                 "prompt": prompt,
#                 "stream": False,
#                 "options": {"temperature": 0.2},
                
#             }
#         )
        
#         # Handle HTTP errors
#         if response.status_code != 200:
#             print(f"API error ({response.status_code}): {response.text[:100]}...")
#             return {"recommendation": "No action needed"}
            
#         response_data = response.json()
#         print("Response from Ollama:", response_data)
#         # Clean response from <think> tags if present
#         recommendation = clean_think_tags(response_data.get('response', '')).strip()
        
#         # Validate response format
#         valid_actions = [
#             "Play music", "Watch funny videos", "Take a break", 
#             "Quick game", "Coding Bot", "Nothing"
#         ]
        
#         if recommendation not in valid_actions:
#             print(f"[Warning] Invalid recommendation: {recommendation}")
#             recommendation = "No action needed"
        
#         # Store in state
#         state.recommendation = recommendation
#         print(f"Recommendation: {recommendation}")
#         return {"recommendation": recommendation}
        
#     except Exception as e:
#         print(f"Error generating recommendation: {str(e)}")
#         return {"recommendation": "No action needed"}



# def send_blocking_message(title, message):
#     MB_OK = 0x0
#     ctypes.windll.user32.MessageBoxW(0, message, title, MB_OK)

# def task_execution_agent(state):
#     recommendation = state.recommendation
#     if "No action" in recommendation:
#         return {"executed": False}

#     send_blocking_message(
#         title="Emotion Assistant",
#         message=f"You seem {state.average_emotion}. Recommendation: {recommendation}"
#     )
#     # This line runs only after user presses OK in the message box
#     execute_task(recommendation)
#     return {"executed": True}