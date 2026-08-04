// MiniJSON.cs —— Unity 官方公共域 JSON 实现（轻量，支持 Dictionary/List/基础类型）
// 来源：https://github.com/Unity-Technologies/unity-plugins （公共域）
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace MiniJSON
{
    public static class Json
    {
        public enum JsonType { NONE, ARRAY, OBJECT, STRING, NUMBER, BOOL, NULL }

        public static object Deserialize(string json)
        {
            if (json == null) return null;
            return Parser.Parse(json);
        }

        public static string Serialize(object obj)
        {
            var sb = new StringBuilder();
            Serializer.Serialize(obj, sb);
            return sb.ToString();
        }

        sealed class Parser
        {
            enum TOKEN { NONE, CURLY_OPEN, CURLY_CLOSE, SQUARED_OPEN, SQUARED_CLOSE,
                         COLON, COMMA, STRING, NUMBER, TRUE, FALSE, NULL }
            const string WORD_TRUE = "true", WORD_FALSE = "false", WORD_NULL = "null";
            static readonly char[] WHITESPACE = { ' ', '\t', '\n', '\r' };
            int index; string json;

            Parser(string json) { this.json = json; }
            public static object Parse(string json)
            {
                if (json == null) return null;
                var p = new Parser(json);
                return p.ParseValue();
            }

            void DiscardWhitespace() { while (index < json.Length && Array.IndexOf(WHITESPACE, json[index]) != -1) index++; }
            char PeekChar() { return json[index]; }
            char NextChar() { char c = json[index]; index++; return c; }
            string NextWord()
            {
                int start = index;
                while (index < json.Length && (char.IsLetterOrDigit(json[index]) || json[index] == '_'))
                    index++;
                return json.Substring(start, index - start);
            }
            TOKEN NextToken()
            {
                DiscardWhitespace();
                if (index == json.Length) return TOKEN.NONE;
                char c = PeekChar();
                switch (c)
                {
                    case '{': return TOKEN.CURLY_OPEN;
                    case '}': NextChar(); return TOKEN.CURLY_CLOSE;
                    case '[': return TOKEN.SQUARED_OPEN;
                    case ']': NextChar(); return TOKEN.SQUARED_CLOSE;
                    case ',': NextChar(); return TOKEN.COMMA;
                    case '"': return TOKEN.STRING;
                    case ':': NextChar(); return TOKEN.COLON;
                    case '0': case '1': case '2': case '3': case '4':
                    case '5': case '6': case '7': case '8': case '9':
                    case '-': return TOKEN.NUMBER;
                }
                string w = NextWord();
                if (w == WORD_TRUE) return TOKEN.TRUE;
                if (w == WORD_FALSE) return TOKEN.FALSE;
                if (w == WORD_NULL) return TOKEN.NULL;
                return TOKEN.NONE;
            }

            object ParseValue()
            {
                TOKEN t = NextToken();
                return ParseByToken(t);
            }
            object ParseByToken(TOKEN t)
            {
                switch (t)
                {
                    case TOKEN.STRING: return ParseString();
                    case TOKEN.NUMBER: return ParseNumber();
                    case TOKEN.CURLY_OPEN: return ParseObject();
                    case TOKEN.SQUARED_OPEN: return ParseArray();
                    case TOKEN.TRUE: return true;
                    case TOKEN.FALSE: return false;
                    case TOKEN.NULL: return null;
                }
                return null;
            }
            string ParseString()
            {
                StringBuilder s = new StringBuilder();
                NextChar(); // "
                bool done = false;
                while (!done)
                {
                    if (index == json.Length) break;
                    char c = NextChar();
                    if (c == '"') { done = true; break; }
                    if (c == '\\')
                    {
                        if (index == json.Length) break;
                        c = NextChar();
                        switch (c)
                        {
                            case '"': s.Append('"'); break;
                            case '\\': s.Append('\\'); break;
                            case '/': s.Append('/'); break;
                            case 'b': s.Append('\b'); break;
                            case 'f': s.Append('\f'); break;
                            case 'n': s.Append('\n'); break;
                            case 'r': s.Append('\r'); break;
                            case 't': s.Append('\t'); break;
                            case 'u':
                                int code = Convert.ToInt32(json.Substring(index, 4), 16);
                                s.Append((char)code); index += 4; break;
                        }
                    }
                    else s.Append(c);
                }
                return s.ToString();
            }
            object ParseNumber()
            {
                int start = index;
                bool done = false;
                while (!done)
                {
                    if (index == json.Length) break;
                    char c = json[index];
                    if (char.IsDigit(c) || c == '.' || c == '-' || c == '+' || c == 'e' || c == 'E')
                        index++;
                    else done = true;
                }
                string num = json.Substring(start, index - start);
                if (num.Contains(".") || num.Contains("e") || num.Contains("E"))
                    return double.Parse(num, CultureInfo.InvariantCulture);
                return long.Parse(num, CultureInfo.InvariantCulture);
            }
            Dictionary<string, object> ParseObject()
            {
                var table = new Dictionary<string, object>();
                NextChar(); // {
                while (true)
                {
                    TOKEN t = NextToken();
                    if (t == TOKEN.NONE) return null;
                    if (t == TOKEN.CURLY_CLOSE) return table;
                    string name = ParseString();
                    if (NextToken() != TOKEN.COLON) return null;
                    NextChar(); // :
                    table[name] = ParseValue();
                }
            }
            List<object> ParseArray()
            {
                var array = new List<object>();
                NextChar(); // [
                while (true)
                {
                    TOKEN t = NextToken();
                    if (t == TOKEN.NONE) return null;
                    if (t == TOKEN.SQUARED_CLOSE) return array;
                    object v = ParseValue();
                    array.Add(v);
                }
            }
        }

        sealed class Serializer
        {
            StringBuilder builder;
            Serializer(StringBuilder sb) { builder = sb; }
            public static void Serialize(object obj, StringBuilder sb)
            {
                var s = new Serializer(sb);
                s.SerializeValue(obj);
            }
            void SerializeValue(object value)
            {
                if (value == null) builder.Append("null");
                else if (value is string) SerializeString((string)value);
                else if (value is bool) builder.Append((bool)value ? "true" : "false");
                else if (value is char) SerializeString(value.ToString());
                else if (value is int || value is long || value is float || value is double || value is decimal)
                    builder.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
                else if (value is IDictionary)
                {
                    builder.Append('{');
                    bool first = true;
                    foreach (DictionaryEntry e in (IDictionary)value)
                    {
                        if (!first) builder.Append(',');
                        SerializeString(e.Key.ToString());
                        builder.Append(':');
                        SerializeValue(e.Value);
                        first = false;
                    }
                    builder.Append('}');
                }
                else if (value is IList)
                {
                    builder.Append('[');
                    bool first = true;
                    foreach (object o in (IList)value)
                    {
                        if (!first) builder.Append(',');
                        SerializeValue(o);
                        first = false;
                    }
                    builder.Append(']');
                }
                else builder.Append("null");
            }
            void SerializeString(string str)
            {
                builder.Append('"');
                char[] chars = str.ToCharArray();
                foreach (char c in chars)
                {
                    switch (c)
                    {
                        case '"': builder.Append("\\\""); break;
                        case '\\': builder.Append("\\\\"); break;
                        case '\b': builder.Append("\\b"); break;
                        case '\f': builder.Append("\\f"); break;
                        case '\n': builder.Append("\\n"); break;
                        case '\r': builder.Append("\\r"); break;
                        case '\t': builder.Append("\\t"); break;
                        default:
                            if (c < ' ') builder.Append("\\u").Append(((int)c).ToString("x4"));
                            else builder.Append(c);
                            break;
                    }
                }
                builder.Append('"');
            }
        }
    }
}
