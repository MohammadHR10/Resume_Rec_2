# Resume Recommender

AI-powered resume analysis and candidate recommendation system using Mistral AI. Available as both a web interface (Streamlit) and REST API (FastAPI) for easy integration.

## Features

- **AI-Powered Analysis**: Uses Mistral AI for intelligent resume evaluation
- **Structured Output**: Returns validated JSON with scores, recommendations, and detailed analysis
- **Dual Interface**: Web UI for direct use, REST API for integration
- **PDF Support**: Extracts text from PDF resumes automatically
- **Comprehensive Evaluation**: Provides scores, strengths, concerns, skills match, and recommendations

## Quick Start

### Prerequisites

1. **Python 3.8+** installed
2. **Mistral AI API Key** - Get one from [Mistral AI](https://mistral.ai/)

### Installation

1. **Clone the repository:**

   ```bash
   git clone <repository-url>
   cd Resume_Rec_2
   ```

2. **Create and activate virtual environment:**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables:**
   Create a `.env` file in the project root:
   ```env
   mistral_api=your_mistral_api_key_here
   ```

## Usage

### Option 1: Web Interface (Streamlit)

**Start the Streamlit app:**

```bash
source venv/bin/activate
streamlit run app.py
```

**Access the app:**

- Open your browser and go to: `http://localhost:8501`
- Upload PDF resumes
- Enter job title, department, and description
- Click "Recommend Candidates" to get AI analysis

**Features:**

- Easy drag-and-drop PDF upload
- Real-time analysis results
- Structured display with scores and recommendations
- Support for multiple resume analysis

### Option 2: REST API (FastAPI)

**Start the API server:**

```bash
source venv/bin/activate
uvicorn main:app --reload
```

**Access the API:**

- API Base URL: `http://localhost:8888`
- Interactive docs: `http://localhost:8888/docs`
- API documentation: `http://localhost:8888/redoc`

**API Endpoint:**

```
POST /recommend
```

**Request Format (multipart/form-data):**

- `job_title` (string): The job position title
- `department` (string): Department (Engineering, Marketing, Design, Data, Other)
- `job_description` (string): Detailed job requirements and description
- `resume_file` (file): PDF file containing the candidate's resume

## Project Structure

```
Resume_Rec_2/
├── app.py              # Streamlit web interface
├── main.py             # FastAPI backend server
├── mistral_client.py   # Mistral AI integration
├── pdf_extract.py      # PDF text extraction utilities
├── test_api.py         # API testing script
├── requirements.txt    # Python dependencies
├── .env                # Environment variables (create this)
└── README.md          # This file
```

## Dependencies

- **FastAPI**: REST API framework
- **Streamlit**: Web interface framework
- **Mistral AI**: AI model for resume analysis
- **pdfminer.six**: PDF text extraction
- **Pydantic**: Data validation and serialization
- **uvicorn**: ASGI server for FastAPI

## Environment Variables

Required environment variables in `.env` file:

```env
mistral_api=your_mistral_api_key_here
```

## License

MIT License - see LICENSE file for details.
