#include "sierrachart.h"
#include <windows.h>
#include <fstream>
#include <vector>
#include <cmath>

SCDLLName("TraderAiz_FootPrint")

SCSF(TraderAiz_FootPrint)
{
    SCInputRef In_ExportHistory = sc.Input[0];
    SCInputRef In_OutputPath    = sc.Input[1];
    SCInputRef In_PipeName      = sc.Input[2];

    // Persistent handle for the real-time Windows Named Pipe
    static HANDLE hPipe = INVALID_HANDLE_VALUE;

    if (sc.SetDefaults)
    {
        sc.GraphName = "TraderAiz Footprint Pipeline";
        sc.AutoLoop = 1; // Process every live tick update
        sc.CalculationPrecedence = LOW_PRECEDENCE;

        In_ExportHistory.Name = "Export Historical Data Now?";
        In_ExportHistory.SetYesNo(0);

        In_OutputPath.Name = "Historical CSV Output Path";
        In_OutputPath.SetString("C:\\SierraChart\\Data\\footprint_history.csv");

        In_PipeName.Name = "Windows Named Pipe Name";
        In_PipeName.SetString("\\\\.\\pipe\\sc_footprint_stream");

        return;
    }

    // --- PHASE 1: ONE-TIME HISTORICAL EXPORT ---
    if (In_ExportHistory.GetYesNo() == 1)
    {
        In_ExportHistory.SetYesNo(0); // Instantly reset switch
        
        std::ofstream OutFile(In_OutputPath.GetString(), std::ios::out | std::ios::trunc);
        if (!OutFile.is_open())
        {
            sc.AddMessageToLog("Error: Cannot open historical output path.", 1);
            return;
        }

        OutFile << "BarIndex,Timestamp,Open,High,Low,Close,PriceLevel,BidVolume,AskVolume,IsBarHigh,IsBarLow\n";
        float Epsilon = sc.TickSize / 2.0f;

        for (int BarIndex = 0; BarIndex < sc.ArraySize; ++BarIndex)
        {
            const s_VolumeAtPriceV2* p_VAPArray = NULL;
            int VAPSize = sc.GetVolumeAtPriceArray(BarIndex, &p_VAPArray);
            if (VAPSize == 0 || p_VAPArray == NULL) continue;

            SCDateTime BarTime = sc.BaseDateTimeIn[BarIndex];
            SCString TimeStr = sc.DateTimeToString(BarTime, TYPE_DATE_TIME);
            float BOpen  = sc.Open[BarIndex];
            float BHigh  = sc.High[BarIndex];
            float BLow   = sc.Low[BarIndex];
            float BClose = sc.Close[BarIndex];

            for (int i = 0; i < VAPSize; ++i)
            {
                float Price = p_VAPArray[i].PriceLevel;
                int IsHigh = (fabs(Price - BHigh) < Epsilon) ? 1 : 0;
                int IsLow  = (fabs(Price - BLow) < Epsilon) ? 1 : 0;

                OutFile << BarIndex << ","
                        << TimeStr.GetChars() << ","
                        << BOpen << ","
                        << BHigh << ","
                        << BLow << ","
                        << BClose << ","
                        << Price << ","
                        << p_VAPArray[i].BidVolume << ","
                        << p_VAPArray[i].AskVolume << ","
                        << IsHigh << ","
                        << IsLow << "\n";
            }
        }
        OutFile.close();
        sc.AddMessageToLog("Historical footprint export completed successfully.", 0);
    }

    // --- PHASE 2: REAL-TIME PIPELINE STREAMING ---
    if (sc.IsUserAllowedRealtimeStreaming && sc.UpdateStartIndex >= sc.ArraySize - 1)
    {
        if (hPipe == INVALID_HANDLE_VALUE)
        {
            hPipe = CreateFileA(In_PipeName.GetString(), GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
            if (hPipe == INVALID_HANDLE_VALUE) return; 
        }

        int CurrentBarIdx = sc.ArraySize - 1;
        const s_VolumeAtPriceV2* p_VAPArray = NULL;
        int VAPSize = sc.GetVolumeAtPriceArray(CurrentBarIdx, &p_VAPArray);
        if (VAPSize == 0 || p_VAPArray == NULL) return;

        float BHigh = sc.High[CurrentBarIdx];
        float BLow  = sc.Low[CurrentBarIdx];
        float Epsilon = sc.TickSize / 2.0f;

        // Packet Structure: START_BAR|BarIndex|Timestamp;Price,BidVol,AskVol,IsHigh,IsLow;...|END_BAR\n
        SCString Packet = "START_BAR|";
        Packet.AppendFormat("%d|%s;", CurrentBarIdx, sc.DateTimeToString(sc.BaseDateTimeIn[CurrentBarIdx], TYPE_DATE_TIME).GetChars());

        for (int i = 0; i < VAPSize; ++i)
        {
            float Price = p_VAPArray[i].PriceLevel;
            int IsHigh = (fabs(Price - BHigh) < Epsilon) ? 1 : 0;
            int IsLow  = (fabs(Price - BLow) < Epsilon) ? 1 : 0;

            Packet.AppendFormat("%.2f,%u,%u,%d,%d;", 
                Price, p_VAPArray[i].BidVolume, p_VAPArray[i].AskVolume, IsHigh, IsLow);
        }
        Packet += "|END_BAR\n";

        DWORD BytesWritten;
        WriteFile(hPipe, Packet.GetChars(), Packet.GetLength(), &BytesWritten, NULL);
    }
}
